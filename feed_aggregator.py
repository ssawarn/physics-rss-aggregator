"""Feed aggregator module for physics RSS feeds."""
import feedparser
import requests
from datetime import datetime, timedelta
import yaml
from typing import Dict, List, Any
import re
import logging

# Set up logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger(__name__)

# Global stats for debugging
FEED_STATS = {}

async def aggregate(topic: str) -> List[Dict[str, Any]]:
    """
    Aggregate RSS feeds for a given topic.
    
    Args:
        topic: The topic to aggregate feeds for (e.g., 'ion-trap', 'quantum-networks')
        
    Returns:
        list: List of feed items with title, abstract, source, and date
    """
    global FEED_STATS
    FEED_STATS = {}  # Reset stats for each request
    
    logger.info(f"Starting aggregation for topic: {topic}")
    
    # Replace dashes with spaces in topic for better keyword matching
    normalized_topic = topic.replace('-', ' ')
    logger.info(f"Normalized topic: '{topic}' -> '{normalized_topic}'")
    
    # Load feeds from feeds.yaml
    try:
        with open('feeds.yaml', 'r') as f:
            feeds_config = yaml.safe_load(f)
    except FileNotFoundError:
        logger.error("feeds.yaml not found")
        return []
    
    # Get feeds from 'normal_feeds' key in YAML config
    topic_feeds = feeds_config.get('normal_feeds', [])
    if not topic_feeds:
        logger.error("No normal_feeds found in feeds.yaml")
        return []
    
    logger.info(f"Found {len(topic_feeds)} RSS feeds to process")
    
    # Calculate date threshold (2 months ago for filtering)
    two_months_ago = datetime.now() - timedelta(days=60)
    six_months_ago = datetime.now() - timedelta(days=180)
    
    logger.info(f"Date filters: 2-month threshold: {two_months_ago}, 6-month threshold: {six_months_ago}")
    
    results = []
    total_raw_items = 0
    total_date_filtered = 0
    total_keyword_filtered = 0
    total_final_items = 0
    
    # Check if this is a combined filter topic
    is_quantum_networks_combined = 'quantum networks' in normalized_topic.lower() and ('ion' in normalized_topic.lower() or 'atom' in normalized_topic.lower())
    logger.info(f"Is quantum networks combined topic: {is_quantum_networks_combined}")
    
    # Process each feed
    for i, feed_url in enumerate(topic_feeds, 1):
        feed_name = feed_url.split('/')[-1] if '/' in feed_url else feed_url
        logger.info(f"\n[{i}/{len(topic_feeds)}] Processing feed: {feed_name}")
        logger.info(f"URL: {feed_url}")
        
        try:
            # Fetch and parse the feed
            response = requests.get(feed_url, timeout=15)
            logger.info(f"HTTP Status: {response.status_code}")
            
            if response.status_code != 200:
                logger.warning(f"HTTP error {response.status_code} for {feed_url}")
                FEED_STATS[feed_name] = {
                    'status': f'HTTP {response.status_code}',
                    'raw_items': 0,
                    'date_filtered': 0,
                    'keyword_filtered': 0,
                    'final_items': 0
                }
                continue
                
            feed = feedparser.parse(response.content)
            
            # Check for RSS parsing errors
            if hasattr(feed, 'bozo') and feed.bozo:
                logger.warning(f"Feed parsing warning for {feed_url}: {getattr(feed, 'bozo_exception', 'Unknown error')}")
            
            raw_items = len(feed.entries)
            logger.info(f"Raw items fetched: {raw_items}")
            total_raw_items += raw_items
            
            date_filtered_count = 0
            keyword_filtered_count = 0
            final_count = 0
            
            for entry in feed.entries:
                # Extract published date
                pub_date = None
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    pub_date = datetime(*entry.published_parsed[:6])
                elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                    pub_date = datetime(*entry.updated_parsed[:6])
                
                # Filter by date (last 6 months for fetching, 2 months for display)
                if pub_date and pub_date < six_months_ago:
                    date_filtered_count += 1
                    continue
                
                # Extract data
                title = entry.get('title', '')
                abstract = entry.get('summary', '') or entry.get('description', '')
                
                # Apply keyword filtering for combined topics
                if is_quantum_networks_combined:
                    text_to_search = (title + ' ' + abstract).lower()
                    has_quantum_network = bool(re.search(r'quantum\s+network', text_to_search))
                    has_ion_or_atom = bool(re.search(r'\b(ion|atom|atomic)\s+(trap|qubit)', text_to_search)) or \
                                     bool(re.search(r'trapped[\s-](ion|atom)', text_to_search))
                    
                    if not (has_quantum_network and has_ion_or_atom):
                        keyword_filtered_count += 1
                        continue
                
                # Check if item passes 2-month filter for final display
                include_in_results = not pub_date or pub_date >= two_months_ago
                
                # Determine source/journal
                source = feed.feed.get('title', '') or entry.get('author', '') or 'Unknown'
                
                item = {
                    'title': title,
                    'abstract': abstract,
                    'source': source,
                    'published': pub_date.isoformat() if pub_date else '',
                    'link': entry.get('link', ''),
                    'published_parsed': entry.get('published_parsed', None)
                }
                
                if include_in_results:
                    results.append(item)
                    final_count += 1
                
            # Store feed statistics
            FEED_STATS[feed_name] = {
                'status': 'SUCCESS',
                'raw_items': raw_items,
                'date_filtered': date_filtered_count,
                'keyword_filtered': keyword_filtered_count,
                'final_items': final_count
            }
            
            total_date_filtered += date_filtered_count
            total_keyword_filtered += keyword_filtered_count
            total_final_items += final_count
            
            logger.info(f"Feed processing complete:")
            logger.info(f"  - Raw items: {raw_items}")
            logger.info(f"  - Date filtered (6mo): {date_filtered_count}")
            logger.info(f"  - Keyword filtered: {keyword_filtered_count}")
            logger.info(f"  - Final items (2mo): {final_count}")
                
        except Exception as e:
            logger.error(f"Error fetching feed {feed_url}: {e}")
            FEED_STATS[feed_name] = {
                'status': f'ERROR: {str(e)}',
                'raw_items': 0,
                'date_filtered': 0,
                'keyword_filtered': 0,
                'final_items': 0
            }
            continue
    
    # Sort by date (newest first)
    results.sort(key=lambda x: x.get('published', ''), reverse=True)
    
    # Log final summary
    logger.info(f"\n=== AGGREGATION SUMMARY ===")
    logger.info(f"Topic: {topic} (normalized: {normalized_topic})")
    logger.info(f"Feeds processed: {len(topic_feeds)}")
    logger.info(f"Total raw items: {total_raw_items}")
    logger.info(f"Date filtered out (6mo): {total_date_filtered}")
    logger.info(f"Keyword filtered out: {total_keyword_filtered}")
    logger.info(f"Final items (2mo): {total_final_items}")
    logger.info(f"============================\n")
    
    return results

def get_feed_stats():
    """Return feed statistics for debugging."""
    return FEED_STATS
