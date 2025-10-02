"""Feed aggregator module for physics RSS feeds."""
import feedparser
import requests
from datetime import datetime, timedelta
import yaml
from typing import Dict, List, Any
import re

async def aggregate(topic: str) -> List[Dict[str, Any]]:
    """
    Aggregate RSS feeds for a given topic.
    
    Args:
        topic: The topic to aggregate feeds for (e.g., 'ion-trap', 'quantum-networks')
        
    Returns:
        list: List of feed items with title, abstract, source, and date
    """
    # Load feeds from feeds.yaml
    try:
        with open('feeds.yaml', 'r') as f:
            feeds_config = yaml.safe_load(f)
    except FileNotFoundError:
        return []
    
    # Get feeds from 'normal_feeds' key in YAML config
    # This allows all RSS feeds to be accessible for every topic
    topic_feeds = feeds_config.get('normal_feeds', [])
    if not topic_feeds:
        return []
    
    # Calculate date threshold (6 months ago)
    six_months_ago = datetime.now() - timedelta(days=180)
    
    results = []
    
    # Check if this is a combined filter topic
    is_quantum_networks_combined = 'quantum-networks' in topic.lower() and ('ion' in topic.lower() or 'atom' in topic.lower())
    
    for feed_url in topic_feeds:
        try:
            # Fetch and parse the feed
            response = requests.get(feed_url, timeout=10)
            feed = feedparser.parse(response.content)
            
            for entry in feed.entries:
                # Extract published date
                pub_date = None
                if hasattr(entry, 'published_parsed') and entry.published_parsed:
                    pub_date = datetime(*entry.published_parsed[:6])
                elif hasattr(entry, 'updated_parsed') and entry.updated_parsed:
                    pub_date = datetime(*entry.updated_parsed[:6])
                
                # Filter by date (last 6 months)
                if pub_date and pub_date < six_months_ago:
                    continue
                
                # Extract data
                title = entry.get('title', '')
                abstract = entry.get('summary', '') or entry.get('description', '')
                
                # For quantum networks + ion/atom topics, filter by keyword matching
                if is_quantum_networks_combined:
                    text_to_search = (title + ' ' + abstract).lower()
                    has_quantum_network = bool(re.search(r'quantum\s+network', text_to_search))
                    has_ion_or_atom = bool(re.search(r'\b(ion|atom|atomic)\s+(trap|qubit)', text_to_search)) or \
                                     bool(re.search(r'trapped[\s-](ion|atom)', text_to_search))
                    
                    if not (has_quantum_network and has_ion_or_atom):
                        continue
                
                # Determine source/journal
                source = feed.feed.get('title', '') or entry.get('author', '') or 'Unknown'
                
                item = {
                    'title': title,
                    'abstract': abstract,
                    'source': source,
                    'published': pub_date.isoformat() if pub_date else '',
                    'link': entry.get('link', '')
                }
                
                results.append(item)
                
        except Exception as e:
            # Log error but continue with other feeds
            print(f"Error fetching feed {feed_url}: {e}")
            continue
    
    # Sort by date (newest first)
    results.sort(key=lambda x: x.get('published', ''), reverse=True)
    
    return results
