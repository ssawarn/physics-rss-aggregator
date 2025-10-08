import os
import asyncio
import json
from datetime import datetime
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, HTMLResponse
from fastapi.middleware.cors import CORSMiddleware
from fastapi.templating import Jinja2Templates
from feed_aggregator import aggregate, aggregate_all, get_feed_stats
from pydantic import BaseModel
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

# Daily cache for /all-rss endpoint
DAILY_CACHE = {
    'items': [],
    'last_updated': None,
    'count': 0
}
# Use asyncio.Lock instead of threading.Lock for async compatibility
DAILY_CACHE_LOCK = asyncio.Lock()

# Favorites file path
FAVORITES_FILE = "favorites.json"
# Use asyncio.Lock instead of threading.Lock for async compatibility
FAVORITES_LOCK = asyncio.Lock()

def canonical(s: str) -> str:
    return s.lower().strip().replace(" ", "-").replace("_", "-")

def normalize_topic(topic: str) -> str:
    """Normalize topic by replacing dashes with spaces for keyword matching."""
    return topic.replace('-', ' ')

def filter_and_group_recent(items, cutoff_days=60):
    from datetime import timedelta
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

async def refresh_daily_cache():
    """Refresh the daily cache by fetching all RSS feeds."""
    global DAILY_CACHE
    try:
        print(f"[{datetime.now()}] Starting daily RSS cache refresh...")
        items = await aggregate_all()
        async with DAILY_CACHE_LOCK:
            DAILY_CACHE['items'] = items
            DAILY_CACHE['last_updated'] = datetime.now().isoformat()
            DAILY_CACHE['count'] = len(items)
        print(f"[{datetime.now()}] Daily cache refresh complete. Total items: {len(items)}")
    except Exception as e:
        print(f"[{datetime.now()}] Error refreshing daily cache: {e}")
        import traceback
        traceback.print_exc()

async def schedule_daily_refresh():
    """Background async task to refresh cache at 1 AM every day."""
    while True:
        now = datetime.now()
        # Calculate next 1 AM
        target_time = now.replace(hour=1, minute=0, second=0, microsecond=0)
        if now >= target_time:
            # If it's already past 1 AM today, schedule for tomorrow
            from datetime import timedelta
            target_time += timedelta(days=1)
        
        sleep_seconds = (target_time - now).total_seconds()
        print(f"[{datetime.now()}] Next cache refresh scheduled at {target_time} (in {sleep_seconds/3600:.2f} hours)")
        await asyncio.sleep(sleep_seconds)
        
        # Refresh the cache
        await refresh_daily_cache()

@app.on_event("startup")
async def startup_event():
    """Initialize cache on startup and start background refresh task.
    
    Async approach:
    1. Immediate refresh on startup (await)
    2. Background task for daily scheduled refresh at 1 AM
    """
    print(f"[{datetime.now()}] Application starting up...")
    
    # Immediate startup refresh (await instead of thread)
    await refresh_daily_cache()
    
    # Start background task for daily refresh at 1 AM
    asyncio.create_task(schedule_daily_refresh())
    
    print(f"[{datetime.now()}] Startup refresh complete and daily scheduler started")

@app.get("/", response_class=HTMLResponse)
async def root(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})

async def _handle_topic_request(topic: str):
    """Shared async logic for both /rss/{topic} and /feed/{topic} endpoints"""
    topic_key = topic.lower().strip()
    # Normalize topic for aggregate function (replace dashes with spaces)
    normalized_topic = normalize_topic(topic_key)
    
    # Check cache first
    if topic_key in CACHE:
        cached_data, cached_time = CACHE[topic_key]
        if time.time() - cached_time < CACHE_TTL:
            # Add fresh feed stats to cached response
            cached_data['feed_stats'] = get_feed_stats()
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
            "items": sum(grouped.values(), []),  # flattened list for backward compat
            "recent_grouped": grouped,
            "feed_stats": get_feed_stats()
        }
    else:
        try:
            items = await aggregate(normalized_topic)
        except Exception as e:
            raise HTTPException(status_code=500, detail=str(e))
        grouped = filter_and_group_recent(items)
        payload = {
            "topic": topic_key,
            "count": sum(len(v) for v in grouped.values()),
            "items": sum(grouped.values(), []),
            "recent_grouped": grouped,
            "feed_stats": get_feed_stats()
        }
    
    CACHE[topic_key] = (payload, time.time())
    return JSONResponse(content=payload)

@app.get("/rss/{topic}")
async def get_rss(topic: str):
    return await _handle_topic_request(topic)

@app.get("/feed/{topic}")
async def get_feed(topic: str):
    return await _handle_topic_request(topic)

@app.get("/debug/{topic}")
async def get_debug_stats(topic: str):
    """Get debugging statistics for a topic."""
    topic_key = topic.lower().strip()
    normalized_topic = normalize_topic(topic_key)
    
    try:
        # Force fresh aggregation for debugging
        items = await aggregate(normalized_topic)
        grouped = filter_and_group_recent(items)
        
        return JSONResponse(content={
            "topic": topic_key,
            "normalized_topic": normalized_topic,
            "feed_stats": get_feed_stats(),
            "total_items": len(items),
            "grouped_count": sum(len(v) for v in grouped.values()),
            "recent_grouped": grouped
        })
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))

@app.get("/all-rss")
async def get_all_rss(force_refresh: bool = False):
    """Get all RSS feed items from daily cache."""
    global DAILY_CACHE
    
    # If force_refresh is requested, refresh the cache
    if force_refresh:
        asyncio.create_task(refresh_daily_cache())
    
    async with DAILY_CACHE_LOCK:
        return JSONResponse(content={
            "count": DAILY_CACHE['count'],
            "items": DAILY_CACHE['items'],
            "last_updated": DAILY_CACHE['last_updated'],
            "status": "success"
        })

@app.post("/refresh-cache")
async def manual_refresh_cache():
    """Manually trigger cache refresh."""
    asyncio.create_task(refresh_daily_cache())
    async with DAILY_CACHE_LOCK:
        return JSONResponse(content={
            "status": "success",
            "message": "Cache refresh triggered",
            "last_updated": DAILY_CACHE['last_updated'],
            "count": DAILY_CACHE['count']
        })

@app.get("/health")
async def health():
    return {"status": "ok"}

# Favorites functionality
class FavoriteRequest(BaseModel):
    article_id: str
    title: str
    link: str
    source: str
    published: str

async def load_favorites():
    """Load favorites from JSON file."""
    try:
        if os.path.exists(FAVORITES_FILE):
            # Use asyncio to read file (stub - in production, use aiofiles)
            # For now, keep synchronous file I/O as it's quick
            with open(FAVORITES_FILE, 'r') as f:
                return json.load(f)
        return []
    except Exception as e:
        print(f"Error loading favorites: {e}")
        return []

async def save_favorites(favorites):
    """Save favorites to JSON file."""
    try:
        # Use asyncio to write file (stub - in production, use aiofiles)
        # For now, keep synchronous file I/O as it's quick
        with open(FAVORITES_FILE, 'w') as f:
            json.dump(favorites, f, indent=2)
    except Exception as e:
        print(f"Error saving favorites: {e}")
        raise HTTPException(status_code=500, detail="Failed to save favorites")

@app.get("/favorites")
async def get_favorites():
    """Get all favorite articles for the user."""
    async with FAVORITES_LOCK:
        favorites = await load_favorites()
        return JSONResponse(content={
            "status": "success",
            "count": len(favorites),
            "favorites": favorites
        })

@app.post("/favorite")
async def add_favorite(request: FavoriteRequest):
    """Add an article to favorites."""
    async with FAVORITES_LOCK:
        favorites = await load_favorites()
        
        # Check if article is already favorited
        if any(fav['article_id'] == request.article_id for fav in favorites):
            return JSONResponse(content={
                "status": "already_exists",
                "message": "Article is already in favorites"
            })
        
        # Add new favorite
        new_favorite = {
            "article_id": request.article_id,
            "title": request.title,
            "link": request.link,
            "source": request.source,
            "published": request.published,
            "favorited_at": datetime.now().isoformat()
        }
        favorites.append(new_favorite)
        await save_favorites(favorites)
        
        return JSONResponse(content={
            "status": "success",
            "message": "Article added to favorites",
            "favorite": new_favorite
        })

@app.post("/unfavorite")
async def remove_favorite(request: FavoriteRequest):
    """Remove an article from favorites."""
    async with FAVORITES_LOCK:
        favorites = await load_favorites()
        
        # Find and remove the favorite
        initial_count = len(favorites)
        favorites = [fav for fav in favorites if fav['article_id'] != request.article_id]
        
        if len(favorites) == initial_count:
            return JSONResponse(content={
                "status": "not_found",
                "message": "Article not found in favorites"
            })
        
        await save_favorites(favorites)
        
        return JSONResponse(content={
            "status": "success",
            "message": "Article removed from favorites"
        })

if __name__ == "__main__":
    import uvicorn
    port = int(os.environ.get("PORT", 8000))
    uvicorn.run(app, host="0.0.0.0", port=port)
