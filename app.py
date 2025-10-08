import os
import asyncio
import threading
import json
from datetime import datetime, time as dtime
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
DAILY_CACHE_LOCK = threading.Lock()

# Favorites file path
FAVORITES_FILE = "favorites.json"
FAVORITES_LOCK = threading.Lock()

def canonical(s: str) -> str:
    return s.lower().strip().replace(" ", "-").replace("_", "-")

def normalize_topic(topic: str) -> str:
    """Normalize topic by replacing dashes with spaces for keyword matching."""
    return topic.replace('-', ' ')

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
                if src not in grouped:
                    grouped[src] = []
                grouped[src].append(item)
    
    # Convert to the format expected by the endpoint
    result = []
    for source, articles in grouped.items():
        result.append({
            "source": source,
            "articles": articles
        })
    
    return result

# Load favorites from file
def load_favorites():
    try:
        with open(FAVORITES_FILE, 'r') as f:
            return json.load(f)
    except (FileNotFoundError, json.JSONDecodeError):
        return []

# Save favorites to file
def save_favorites(favorites):
    with open(FAVORITES_FILE, 'w') as f:
        json.dump(favorites, f, indent=2)

class CacheKey(BaseModel):
    source: str = "all"
    topic: str = "all"
    hours: int = 24
    keywords: str = ""

@app.get("/", response_class=HTMLResponse)
def read_root(request: Request):
    """Serve the main frontend HTML page."""
    return templates.TemplateResponse("index.html", {"request": request})

@app.get("/stats")
def get_stats():
    """Get RSS feed statistics."""
    try:
        stats = get_feed_stats()
        return JSONResponse(content=stats)
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Error getting stats: {e}")

@app.get("/cache-info")
def get_cache_info():
    """Get current cache information."""
    cache_sizes = {k: len(v) if isinstance(v, list) else 1 for k, v in CACHE.items()}
    return {
        "cache_keys": list(CACHE.keys()),
        "cache_sizes": cache_sizes,
        "prefetch_keys": list(PREFETCH.keys()),
        "prefetch_last_update": PREFETCH_TS
    }

async def refresh_daily_cache():
    """Refresh the daily cache with all items."""
    global DAILY_CACHE
    
    try:
        # Get all items from feed aggregator
        all_items = await asyncio.to_thread(aggregate_all)
        
        with DAILY_CACHE_LOCK:
            DAILY_CACHE['items'] = all_items
            DAILY_CACHE['last_updated'] = dtime.time()
            DAILY_CACHE['count'] = len(all_items)
            
        print(f"Daily cache refreshed with {len(all_items)} items")
    except Exception as e:
        print(f"Error refreshing daily cache: {e}")

@app.get("/refresh-cache")
async def manual_refresh_cache():
    """Manually refresh the daily cache."""
    await refresh_daily_cache()
    
    with DAILY_CACHE_LOCK:
        return JSONResponse(content={
            "message": "Cache refreshed successfully",
            "count": DAILY_CACHE['count'],
            "last_updated": DAILY_CACHE['last_updated']
        })

@app.get("/recent")
def get_recent(source: str = "all", topic: str = "all", hours: int = 24, keywords: str = "", group_by_source: bool = False):
    """Get recent RSS items, optionally grouped by source."""
    global PREFETCH, PREFETCH_TS
    
    current_time = time.time()
    
    # Create cache key
    cache_key = f"{source}_{topic}_{hours}_{keywords}"
    
    # Check if we have cached data and it's still valid
    if cache_key in CACHE and (current_time - CACHE[cache_key].get('timestamp', 0)) < CACHE_TTL:
        items = CACHE[cache_key]['data']
    else:
        # Get fresh data
        try:
            items = aggregate(source, topic, hours, keywords)
            # Cache the data with timestamp
            CACHE[cache_key] = {
                'data': items,
                'timestamp': current_time
            }
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"Error aggregating feeds: {e}")
    
    if group_by_source:
        # Group items by source
        result = filter_and_group_recent(items, cutoff_days=hours//24 if hours >= 24 else 1)
        return JSONResponse(content={
            "items": result,
            "last_updated": current_time
        })
    else:
        # Return flat list as before
        return JSONResponse(content=items)

@app.get("/all-rss")
def get_all_rss(force_refresh: bool = False):
    """Get all RSS feed items from daily cache, grouped by source."""
    global DAILY_CACHE
    
    # If force_refresh is requested, refresh the cache
    if force_refresh:
        asyncio.create_task(refresh_daily_cache())
    
    with DAILY_CACHE_LOCK:
        # Group the items by source
        grouped_items = filter_and_group_recent(DAILY_CACHE['items'], cutoff_days=9999)  # Include all items
        
        return JSONResponse(content={
            "items": grouped_items,
            "last_updated": str(DAILY_CACHE['last_updated']) if DAILY_CACHE['last_updated'] else None
        })

@app.get("/favorites")
def get_favorites():
    """Get user's favorite articles."""
    with FAVORITES_LOCK:
        favorites = load_favorites()
    return JSONResponse(content={"favorites": favorites})

@app.post("/favorite")
def add_favorite(request: dict):
    """Add an article to favorites."""
    required_fields = ['title', 'link', 'source']
    
    if not all(field in request for field in required_fields):
        raise HTTPException(status_code=400, detail="Missing required fields: title, link, source")
    
    with FAVORITES_LOCK:
        favorites = load_favorites()
        
        # Check if article is already favorited
        if any(fav['link'] == request['link'] for fav in favorites):
            raise HTTPException(status_code=400, detail="Article already in favorites")
        
        # Add timestamp
        request['favorited_at'] = time.time()
        favorites.append(request)
        
        save_favorites(favorites)
    
    return JSONResponse(content={"message": "Article added to favorites", "article": request})

@app.delete("/unfavorite")
def remove_favorite(request: dict):
    """Remove an article from favorites."""
    if 'link' not in request:
        raise HTTPException(status_code=400, detail="Missing required field: link")
    
    with FAVORITES_LOCK:
        favorites = load_favorites()
        
        # Find and remove the article
        original_length = len(favorites)
        favorites = [fav for fav in favorites if fav['link'] != request['link']]
        
        if len(favorites) == original_length:
            raise HTTPException(status_code=404, detail="Article not found in favorites")
        
        save_favorites(favorites)
    
    return JSONResponse(content={"message": "Article removed from favorites"})

# Background task to refresh cache periodically
async def background_cache_refresh():
    """Background task to refresh cache periodically."""
    while True:
        await asyncio.sleep(PREFETCH_INTERVAL_SECONDS)
        await refresh_daily_cache()

@app.on_event("startup")
async def startup_event():
    """Start background tasks on startup."""
    # Initial cache refresh
    await refresh_daily_cache()
    
    # Start background refresh task
    asyncio.create_task(background_cache_refresh())

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
