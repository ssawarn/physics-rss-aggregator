import asyncio
import hashlib
import html
import os
import re
import time
from typing import List, Dict, Any, Optional, Tuple

import aiohttp
import feedparser
import yaml
from dateutil import parser as dateparser
from fastapi import FastAPI, Query, HTTPException
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from jinja2 import Environment, FileSystemLoader, select_autoescape

# -----------------------
# Config and templates
# -----------------------
HERE = os.path.dirname(os.path.abspath(__file__))
TEMPLATES = Environment(
    loader=FileSystemLoader(os.path.join(HERE, "templates")),
    autoescape=select_autoescape(["html", "xml"])
)

DEFAULT_FEEDS_FILE = os.path.join(HERE, "feeds.yaml")
EXAMPLE_FEEDS_FILE = os.path.join(HERE, "feeds.example.yaml")


def load_config():
    path = DEFAULT_FEEDS_FILE if os.path.exists(DEFAULT_FEEDS_FILE) else EXAMPLE_FEEDS_FILE
    with open(path, "r", encoding="utf-8") as f:
        data = yaml.safe_load(f) or {}
    normal = data.get("normal_feeds", [])
    arxiv = data.get("arxiv", {}) or {}
    return {
        "normal_feeds": normal,
        "arxiv": {
            "max_results": int(arxiv.get("max_results", 75)),
            "sort_by": arxiv.get("sort_by", "submittedDate"),
            "sort_order": arxiv.get("sort_order", "descending"),
        }
    }


CONFIG = load_config()

# -----------------------
# In-memory cached store (24h prefetch + 5 min query cache)
# -----------------------
class TTLCache:
    def __init__(self, ttl_seconds: int = 300):
        self.ttl = ttl_seconds
        self.store: Dict[str, Tuple[float, Any]] = {}

    def get(self, key: str):
        item = self.store.get(key)
        if not item:
            return None
        ts, value = item
        if time.time() - ts > self.ttl:
            self.store.pop(key, None)
            return None
        return value

    def set(self, key: str, value: Any):
        self.store[key] = (time.time(), value)


CACHE = TTLCache(ttl_seconds=300)
# Global 24h prefetch store: topic -> payload
PREFETCH: Dict[str, Dict[str, Any]] = {}
PREFETCH_TS: float = 0.0
PREFETCH_INTERVAL_SECONDS = 24 * 60 * 60

# -----------------------
# Utilities - keyword normalization and matching
# -----------------------
WHITESPACE = re.compile(r"\s+")
TAG_RE = re.compile(r"<[^>]+>")
PUNCT_HYPHEN_SPACE = re.compile(r"[-_+/]+")
NON_ALNUM = re.compile(r"[^a-z0-9]+")


def strip_html(text: str) -> str:
    return TAG_RE.sub("", text or "")


def normalize_text(s: Optional[str]) -> str:
    if not s:
        return ""
    return WHITESPACE.sub(" ", strip_html(html.unescape(s))).strip()


def canonical(s: str) -> str:
    # Lowercase, replace hyphens/underscores/slashes with spaces, collapse, then remove non-alnum for fuzzy contains
    s = s.lower()
    s = PUNCT_HYPHEN_SPACE.sub(" ", s)
    s = WHITESPACE.sub(" ", s).strip()
    return s


def fuzzy_key(s: str) -> str:
    # Aggressive normalization for contains checks
    s = canonical(s)
    return NON_ALNUM.sub("", s)


def keyword_variants(term: str) -> List[str]:
    # Build related forms: hyphen/space/joined, plural/singular naive
    t = term.strip()
    forms = set()
    # basic canonical
    forms.add(canonical(t))
    # hyphen/space/joined
    forms.add(canonical(t.replace("-", " ")))
    forms.add(canonical(t.replace(" ", "-")))
    forms.add(canonical(t.replace(" ", "")))
    # naive plural/singular
    if t.endswith("s"):
        forms.add(canonical(t[:-1]))
    else:
        forms.add(canonical(t + "s"))
    return [f for f in forms if f]


def build_keywords(query: str) -> List[str]:
    parts: List[str] = []
    # honor quoted phrases, otherwise split by whitespace
    for tok in re.findall(r'"[^"]+"|\S+', query):
        if tok.startswith('"') and tok.endswith('"') and len(tok) > 1:
            parts.append(tok[1:-1].strip())
        else:
            parts.append(tok)
    variants = set()
    for p in parts:
        for v in keyword_variants(p):
            variants.add(v)
    # Also keep aggressive fuzzy keys for substring matching
    fuzzy = {fuzzy_key(v) for v in variants}
    return sorted(variants), sorted(fuzzy)


def match_entry(entry_text: str, keyword_forms: List[str], fuzzy_forms: List[str]) -> bool:
    if not keyword_forms:
        return True
    hay_can = canonical(entry_text)
    hay_fuz = fuzzy_key(entry_text)
    # strong: all keywords must appear (AND semantics) in canonical text
    if all(k in hay_can for k in keyword_forms):
        return True
    # fallback: any fuzzy form appears in fuzzy haystack
    return any(f in hay_fuz for f in fuzzy_forms)


def dedupe_key(title: str, link: str) -> str:
    base = (title or "").lower().strip() + "|" + (link or "").lower().strip()
    return hashlib.sha1(base.encode("utf-8")).hexdigest()


def parse_date(entry) -> float:
    for key in ("published", "updated", "created"):
        val = getattr(entry, key, None) or (entry.get(key) if isinstance(entry, dict) else None)
        if not val:
            continue
        try:
            return dateparser.parse(val).timestamp()
        except Exception:
            continue
    for key in ("published_parsed", "updated_parsed"):
        val = entry.get(key) if isinstance(entry, dict) else getattr(entry, key, None)
        if val:
            try:
                return time.mktime(val)
            except Exception:
                pass
    return time.time()

# -----------------------
# Fetching
# -----------------------
ARXIV_API = "http://export.arxiv.org/api/query"


def build_arxiv_url(query: str, max_results: int, sort_by: str, sort_order: str) -> str:
    keyword_forms, _ = build_keywords(query)
    tokens = keyword_forms or [query.strip().lower()]
    # quoted phrases if contain spaces
    terms = []
    for t in tokens:
        if " " in t:
            terms.append(f'"{t}"')
        else:
            terms.append(t)
    q = " AND ".join(terms)
    q = f"all:({q})"
    from urllib.parse import urlencode
    params = {
        "search_query": q,
        "start": 0,
        "max_results": max_results,
        "sortBy": sort_by,
        "sortOrder": sort_order,
    }
    return f"{ARXIV_API}?{urlencode(params)}"


async def fetch_text(session: aiohttp.ClientSession, url: str, timeout: int = 20) -> str:
    async with session.get(url, timeout=timeout, headers={"User-Agent": "physics-topic-aggregator/1.1"}) as resp:
        resp.raise_for_status()
        return await resp.text()


async def fetch_arxiv(session: aiohttp.ClientSession, topic: str, max_results: int, sort_by: str, sort_order: str) -> List[Dict[str, Any]]:
    url = build_arxiv_url(topic, max_results, sort_by, sort_order)
    text = await fetch_text(session, url)
    parsed = feedparser.parse(text)
    out = []
    for e in parsed.entries:
        title = normalize_text(getattr(e, "title", ""))
        summary = normalize_text(getattr(e, "summary", ""))
        link = getattr(e, "link", "") or (e.links[0].href if getattr(e, "links", None) else "")
        authors = ", ".join(a.get("name") for a in getattr(e, "authors", []) if a.get("name"))
        out.append({
            "source": "arXiv",
            "title": title,
            "summary": summary,
            "link": link,
            "authors": authors,
            "published_ts": parse_date(e),
        })
    return out


async def fetch_and_filter_feed(session: aiohttp.ClientSession, url: str, keyword_forms: List[str], fuzzy_forms: List[str]) -> List[Dict[str, Any]]:
    try:
        text = await fetch_text(session, url)
    except Exception:
        return []
    parsed = feedparser.parse(text)
    items = []
    for e in parsed.entries:
        title = normalize_text(getattr(e, "title", ""))
        summary = normalize_text(getattr(e, "summary", "")) or normalize_text(getattr(e, "description", ""))
        link = getattr(e, "link", "") or (e.links[0].href if getattr(e, "links", None) else "")
        blob = f"{title}\n{summary}"
        if match_entry(blob, keyword_forms, fuzzy_forms):
            items.append({
                "source": url,
                "title": title,
                "summary": summary,
                "link": link,
                "authors": ", ".join(a.get("name") for a in getattr(e, "authors", []) if a.get("name")) if getattr(e, "authors", None) else "",
                "published_ts": parse_date(e),
            })
    return items


async def aggregate(topic: str) -> List[Dict[str, Any]]:
    cfg = CONFIG
    keyword_forms, fuzzy_forms = build_keywords(topic)
    async with aiohttp.ClientSession() as session:
        arxiv_task = fetch_arxiv(
            session,
            topic,
            max_results=cfg["arxiv"]["max_results"],
            sort_by=cfg["arxiv"]["sort_by"],
            sort_order=cfg["arxiv"]["sort_order"]
        )
        feed_tasks = [
            fetch_and_filter_feed(session, url, keyword_forms, fuzzy_forms) for url in cfg["normal_feeds"]
        ]
        results = await asyncio.gather(arxiv_task, *feed_tasks, return_exceptions=True)

    all_items: List[Dict[str, Any]] = []
    for r in results:
        if isinstance(r, Exception):
            continue
        all_items.extend(r)

    seen = set()
    deduped = []
    for it in all_items:
        k = dedupe_key(it["title"], it["link"])
        if k in seen:
            continue
        seen.add(k)
        deduped.append(it)

    deduped.sort(key=lambda x: x.get("published_ts", 0), reverse=True)
    return deduped


# -----------------------
# Background prefetch every 24 hours
# -----------------------
DEFAULT_TOPICS = [
    # Seed topics commonly used in this domain; can be overridden by queries at runtime
    "ion traps",
    "quantum networks",
    "cavity qed",
]


async def prefetch_all_topics(topics: List[str]):
    global PREFETCH, PREFETCH_TS
    combined: Dict[str, Dict[str, Any]] = {}
    for t in topics:
        try:
            items = await aggregate(t)
            combined[t.lower().strip()] = {"topic": t, "count": len(items), "items": items}
        except Exception:
            combined[t.lower().strip()] = {"topic": t, "count": 0, "items": []}
    PREFETCH = combined
    PREFETCH_TS = time.time()


async def prefetch_loop():
    # Run immediately at startup, then every 24 hours
    await prefetch_all_topics(DEFAULT_TOPICS)
    while True:
        await asyncio.sleep(PREFETCH_INTERVAL_SECONDS)
        await prefetch_all_topics(DEFAULT_TOPICS)


# -----------------------
# FastAPI app
# -----------------------
app = FastAPI(title="Physics Topic RSS Aggregator", version="1.1")

if os.path.isdir(os.path.join(HERE, "static")):
    app.mount("/static", StaticFiles(directory=os.path.join(HERE, "static")), name="static")


@app.on_event("startup")
async def on_startup():
    # Kick off background prefetch task
    asyncio.create_task(prefetch_loop())


@app.get("/", response_class=HTMLResponse)
def home():
    tmpl = TEMPLATES.get_template("index.html")
    return tmpl.render()


@app.get("/api/search")
async def api_search(topic: str = Query(..., min_length=1, max_length=100)):
    topic_key = topic.strip().lower()
    if not topic_key:
        raise HTTPException(status_code=400, detail="Empty topic")

    # quick cache
    cached = CACHE.get(topic_key)
    if cached:
        return JSONResponse(content=cached)

    # If we have a recent prefetch for this exact/related topic, reuse as baseline
    baseline = None
    if PREFETCH and (time.time() - PREFETCH_TS) < PREFETCH_INTERVAL_SECONDS * 1.5:
        # direct hit
        baseline = PREFETCH.get(topic_key)
        if not baseline:
            # try a loose match across prefetched topics
            key_can = canonical(topic_key)
            for k, payload in PREFETCH.items():
                if canonical(k) in key_can or key_can in canonical(k):
                    baseline = payload
                    break

    if baseline:
        payload = baseline
    else:
        try:
            items = await aggregate(topic_key)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
        payload = {"topic": topic_key, "count": len(items), "items": items}

    CACHE.set(topic_key, payload)
    return JSONResponse(content=payload)
