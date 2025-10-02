import os
import asyncio
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from feed_aggregator import aggregate
import time

app = FastAPI()

# Setup Jinja2 templates
templates = Jinja2Templates(directory="templates")

# Add CORS middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Simple in-memory cache
CACHE = {}
CACHE_TTL = 3600  # 1 hour

# Prefetch configuration
PREFETCH_INTERVAL_SECONDS = int(os.environ.get("PREFETCH_INTERVAL_SECONDS", 28800))  # 8h default
PREFETCH = {}
PREFETCH_TS = 0

def canonical(s: str) -> str:
    return s.lower().strip().replace(" ", "-").replace("_", "-")

def filter_and_group_recent(items, cutoff_days=60):
    from datetime import datetime, timedelta
    cutoff = datetime.now() - timedelta(days=cutoff_days)
    grouped = {}
    for item in items:
        pub = item.get("published_parsed")
        if pub:
            dt = datetime(*pub[:6])
            if dt >= cutoff:
                src = item.get("source", "Unknown")
                grouped.setdefault(src, []).append(item)
    return grouped

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/rss/{topic}")
def get_rss(topic: str):
    topic_key = topic.lower().strip()
    
    # Check cache first
    if topic_key in CACHE:
        cached_data, cached_time = CACHE[topic_key]
        if time.time() - cached_time < CACHE_TTL:
            return JSONResponse(content=cached_data)
    
    # baseline
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
        # Use prefetched items but apply 2-month filter again (fresh cutoff)
        grouped = filter_and_group_recent(baseline.get("items", []))
        payload = {
            "topic": baseline.get("topic", topic_key),
            "count": sum(len(v) for v in grouped.values()),
            "items": sum(grouped.values(), []),   # flattened list for backward compat
            "recent_grouped": grouped,
        }
    else:
        try:
            items = asyncio.run(aggregate(topic_key))
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
        grouped = filter_and_group_recent(items)
        payload = {
            "topic": topic_key,
            "count": sum(len(v) for v in grouped.values()),
            "items": sum(grouped.values(), []),
            "recent_grouped": grouped,
        }
    CACHE[topic_key] = (payload, time.time())
    return JSONResponse(content=payload)

@app.get("/health")
def health():
    return {"status": "ok"}

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
