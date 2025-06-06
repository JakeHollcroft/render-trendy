from flask import Flask, render_template, request, jsonify, make_response
from flask_sqlalchemy import SQLAlchemy
from transformers import pipeline
import os
import hashlib
import random
import threading
import time
import logging
from flask_socketio import SocketIO, join_room, leave_room, send, emit, rooms
from datetime import datetime, timezone, date
import re
from collections import Counter
from bs4 import BeautifulSoup
import requests
import uuid
from urllib.parse import urljoin, urlparse
from datetime import datetime, timezone, date, timedelta
from flask_cors import CORS

logging.basicConfig(level=logging.DEBUG, format='%(asctime)s %(levelname)s:%(name)s: %(message)s')
logger = logging.getLogger(__name__)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'your-secret-key-here')  # Set in Render
socketio = SocketIO(
    app,
    cors_allowed_origins=[
        "https://trendiinow.com",
        "https://www.trendiinow.com",
        "https://trendy-wqzi.onrender.com",
        "http://localhost:5000"
    ],
    async_mode='eventlet',
    logger=True,
    engineio_logger=True,
    ping_timeout=60,
    ping_interval=25
)

db_path = '/opt/render/data/trendy.db' if os.getenv('RENDER') else os.path.join(os.path.abspath(os.path.dirname(__file__)), 'trendy.db')
if os.getenv('RENDER'):
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
app.config['SQLALCHEMY_DATABASE_URI'] = f'sqlite:///{db_path}'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

class Trend(db.Model):
    id = db.Column(db.String, primary_key=True)
    title = db.Column(db.String, nullable=False)
    image = db.Column(db.String)
    description = db.Column(db.Text)
    link = db.Column(db.String)
    source = db.Column(db.String)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))

class Vote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    trend_id = db.Column(db.String, db.ForeignKey('trend.id'), nullable=False)
    ip_address = db.Column(db.String, nullable=False)
    vote_type = db.Column(db.String, nullable=False)
    timestamp = db.Column(db.DateTime, default=lambda: datetime.now(timezone.utc))
    __table_args__ = (
        db.UniqueConstraint('trend_id', 'ip_address', name='unique_vote_per_ip'),
    )

with app.app_context():
    db.create_all()
    logger.debug(f"Trend table count: {Trend.query.count()}")
    logger.debug(f"Vote table count: {Vote.query.count()}")

global_trends = []
last_fetch_time = None

STOP_WORDS = {
    'the', 'and', 'for', 'with', 'from', 'this', 'that', 'are', 'was', 'were', 'has', 'have', 'had',
    'but', 'not', 'all', 'any', 'some', 'what', 'when', 'where', 'which', 'who', 'why', 'how'
}

MOOD_KEYWORDS = {
    'Controversial': ['controversy', 'debate', 'scandal', 'dispute', 'conflict', 'outrage'],
    'Wholesome': ['heartwarming', 'kind', 'positive', 'uplifting', 'inspiring', 'charity'],
    'Breaking': ['breaking', 'urgent', 'alert', 'news', 'emergency', 'update'],
    'Weird': ['strange', 'odd', 'unusual', 'bizarre', 'weird', 'quirky'],
    'Funny': ['funny', 'hilarious', 'comedy', 'joke', 'meme', 'lol'],
    'Exciting': ['thrilling', 'exciting', 'epic', 'amazing', 'breakthrough']
}

try:
    logger.debug("Loading t5-small pipeline")
    summarizer = pipeline("summarization", model="t5-small", device="cpu")
    logger.debug("t5-small pipeline loaded")
except Exception as e:
    logger.error(f"Failed to load t5-small: {e}", exc_info=True)
    summarizer = None

def generate_mood_tags(trend):
    try:
        title = str(trend.get("title") or "").lower()
        description = str(trend.get("description") or "").lower()
        text = f"{title} {description}"
        tags = []
        for mood, keywords in MOOD_KEYWORDS.items():
            if any(keyword in text for keyword in keywords):
                tags.append(mood)
        return tags if tags else ['Trending']
    except Exception as e:
        logger.error(f"Error generating mood tags: {e}", exc_info=True)
        return ['Trending']

def generate_summary(trend):
    try:
        title = str(trend.get("title") or "Untitled")
        description = str(trend.get("description") or "")
        source = str(trend.get("source") or "Unknown")
        text = f"{title}. {description}".strip()
        text = re.sub(r'#\w+', '', text)
        text = re.sub(r'\s+', ' ', text).strip(' .|')
        input_length = len(text.split())
        max_length = min(100, max(20, input_length * 2))
        min_length = min(20, max(5, input_length // 2))
        logger.debug(f"Summary input: length={input_length}, max={max_length}, min={min_length}")
        if input_length < 5:
            summary_text = f"'{title}' is trending on {source}."
        else:
            result = summarizer(text, max_length=max_length, min_length=min_length, do_sample=False, truncation=True)
            summary_text = result[0]['summary_text'].strip()
        all_text = f"{title} {description} {summary_text}".lower()
        words = re.findall(r'\w+', all_text)
        keywords = [word for word in words if len(word) > 3 and word not in STOP_WORDS]
        keyword_counts = Counter(keywords).most_common(3)
        selected_keywords = [kw for kw, _ in keyword_counts] or ['trending', source.lower()]
        hashtags = " ".join(f"#{kw.capitalize()}" for kw in selected_keywords)
        meta_keywords = ", ".join(selected_keywords)
        meta_description = f"{summary_text[:160]}{'...' if len(summary_text) > 160 else ''}"
        return {
            "text": summary_text,
            "hashtags": hashtags,
            "meta_description": meta_description,
            "meta_keywords": meta_keywords
        }
    except Exception as e:
        logger.error(f"Error generating summary: {e}", exc_info=True)
        source = str(trend.get("source") or "Unknown")
        title = str(trend.get("title") or "Untitled")
        keywords = [kw for kw in title.lower().split() if len(kw) > 3 and kw not in STOP_WORDS][:3] or ['trending', source.lower()]
        hashtags = " ".join(f"#{kw.capitalize()}" for kw in keywords)
        meta_keywords = ", ".join(keywords)
        fallback_summary = f"'{title}' is trending on {source}."
        meta_description = f"{fallback_summary[:160]}{'...' if len(fallback_summary) > 160 else ''}"
        return {
            "text": fallback_summary,
            "hashtags": hashtags,
            "meta_description": meta_description,
            "meta_keywords": meta_keywords
        }

def generate_stable_id(trend):
    title = str(trend.get("title", "")).strip().lower()
    title = re.sub(r'[^\w\s]', '', title)
    title = re.sub(r'\s+', ' ', title).strip()
    link = str(trend.get("link", "")).strip()
    try:
        parsed = urlparse(link)
        clean_link = f"{parsed.scheme}://{parsed.netloc}{parsed.path}".rstrip('/')
        if not clean_link.startswith(('http://', 'https://')):
            clean_link = f"https://{clean_link.lstrip('/')}"
    except ValueError as e:
        logger.warning(f"Invalid URL in generate_stable_id: {link}, error: {e}")
        clean_link = ""
    key = (title + clean_link).encode("utf-8")
    trend_id = hashlib.md5(key).hexdigest()
    logger.debug(f"ID for title: '{title}', link: '{clean_link}' -> {trend_id}")
    return trend_id

def time_ago(timestamp):
    try:
        if isinstance(timestamp, str):
            past = datetime.fromisoformat(timestamp.replace('Z', '+00:00')).replace(tzinfo=timezone.utc)
        else:
            past = timestamp.replace(tzinfo=timezone.utc)
        now = datetime.now(timezone.utc)
        diff = now - past
        seconds = diff.total_seconds()
        if seconds < 60:
            return f"{int(seconds)} seconds ago"
        elif seconds < 3600:
            return f"{int(seconds // 60)} minutes ago"
        elif seconds < 86400:
            return f"{int(seconds // 3600)} hours ago"
        else:
            return f"{int(seconds // 86400)} days ago"
    except Exception as e:
        logger.error(f"Error in time_ago: {e}", exc_info=True)
        return "Unknown time"

app.jinja_env.globals.update(time_ago=time_ago, current_year=datetime.now().year)

def get_trend_of_the_day(trends):
    if not trends:
        return None
    today = date.today().isoformat()
    random.seed(today)
    return random.choice(trends)

def cleanup_old_trends():
    try:
        threshold = datetime.now(timezone.utc) - timedelta(days=7)
        deleted = Trend.query.filter(Trend.timestamp < threshold).delete()
        db.session.commit()
        logger.debug(f"Cleaned {deleted} old trends")
    except Exception as e:
        logger.error(f"Error cleaning trends: {e}", exc_info=True)
        db.session.rollback()

def get_hacker_news():
    url = 'https://news.ycombinator.com/'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/112.0.0.0'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        items = soup.select('.athing')
        results = []
        for item in items[:25]:
            title_tag = item.select_one('.titleline')
            if not title_tag:
                continue
            title = title_tag.text.strip()
            link = title_tag.find('a')['href']
            if link.startswith('item?id='):
                link = urljoin(url, link)
            elif not link.startswith(('http://', 'https://')):
                link = 'https://' + link.lstrip('/')
            trend = {
                'title': title,
                'description': '',
                'link': link,
                'source': 'From Hacker News',
                'source_class': 'HackerTrending',
                'image': '/static/images/default_trendy.png',
                'video': None
            }
            trend['id'] = generate_stable_id(trend)
            results.append(trend)
        logger.debug(f"Hacker News: Fetched {len(results)} trends")
        return results
    except Exception as e:
        logger.error(f"Error fetching Hacker News trends: {e}", exc_info=True)
        return []

def get_github_trending():
    url = 'https://github.com/trending'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/112.0.0.0'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        items = soup.select('article.Box-row')
        results = []
        for item in items[:25]:
            repo = item.select_one('h2').text.strip().replace('\n', '').replace(' ', '')
            link = 'https://github.com' + item.select_one('h2 a')['href']
            description_tag = item.select_one('p')
            description = description_tag.text.strip() if description_tag else ''
            title = f"{repo}"
            trend = {
                'title': title,
                'description': description,
                'link': link,
                'source': 'From GitHub',
                'source_class': 'GithubTrending',
                'image': '/static/images/default_trendy.png',
                'video': None,
            }
            trend['id'] = generate_stable_id(trend)
            results.append(trend)
        logger.debug(f"GitHub: Fetched {len(results)} trends")
        return results
    except Exception as e:
        logger.error(f"Error fetching GitHub trends: {e}", exc_info=True)
        return []

def get_reddit_top():
    headers = {'User-agent': 'TrendyScraper 1.0'}
    url = 'https://www.reddit.com/r/popular/top.json?limit=25'
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        json_data = response.json()
        results = []
        for item in json_data.get('data', {}).get('children', [])[:25]:
            data = item['data']
            reddit_url = 'https://reddit.com' + data.get('permalink', '')
            title = data.get('title', 'No title')
            description = data.get('selftext', '')
            video_url = None
            image_url = None
            media = data.get('secure_media') or data.get('media')
            if media and 'reddit_video' in media:
                video_url = media['reddit_video'].get('fallback_url')
            if not video_url and data.get('preview'):
                reddit_video_preview = data['preview'].get('reddit_video_preview')
                if reddit_video_preview:
                    video_url = reddit_video_preview.get('fallback_url')
            if not video_url and media and media.get('type', '').startswith('gif'):
                reddit_video = media.get('reddit_video')
                if reddit_video:
                    video_url = reddit_video.get('fallback_url')
            if not video_url and data.get('thumbnail', '').startswith('http'):
                image_url = data['thumbnail']
            if not video_url and not image_url and data.get('preview'):
                images = data['preview'].get('images')
                if images and len(images) > 0:
                    image_url = images[0].get('source', {}).get('url')
                    if image_url:
                        image_url = image_url.replace('&amp;', '&')
            trend = {
                'title': title,
                'description': description,
                'link': reddit_url,
                'source': 'From Reddit',
                'source_class': 'RedditTrending',
                'image': image_url if image_url else '/static/images/default_trendy.png',
                'video': video_url,
            }
            trend['id'] = generate_stable_id(trend)
            results.append(trend)
        logger.debug(f"Reddit: Fetched {len(results)} trends")
        return results
    except Exception as e:
        logger.error(f"Error fetching Reddit trends: {e}", exc_info=True)
        return []

def get_techcrunch():
    url = 'https://techcrunch.com/'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/112.0.0.0'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        items = soup.select('a.post-block__title__link')
        results = []
        for item in items[:25]:
            title = item.get_text(strip=True)
            link = item['href']
            parent = item.find_parent('div', class_='post-block')
            description_tag = parent.select_one('.post-block__content') if parent else None
            description = description_tag.get_text(strip=True) if description_tag else ''
            image_tag = parent.select_one('img') if parent else None
            image = image_tag['src'] if image_tag and image_tag.has_attr('src') else '/static/images/default_trendy.svg'
            trend = {
                'title': title,
                'description': description,
                'link': link,
                'source': 'From TechCrunch',
                'source_class': 'TechcrunchTrending',
                'image': image,
                'video': None
            }
            trend['id'] = generate_stable_id(trend)
            results.append(trend)
        logger.debug(f"TechCrunch: Fetched {len(results)} trends")
        return results
    except Exception as e:
        logger.error(f"Error fetching TechCrunch trends: {e}", exc_info=True)
        return []

def get_stackoverflow_trending():
    url = 'https://stackoverflow.com/questions?tab=Hot'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/112.0.0.0'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        items = soup.select('.s-post-summary')
        results = []
        for item in items[:25]:
            title_tag = item.select_one('.s-link')
            if not title_tag:
                continue
            title = title_tag.text.strip()
            link = urljoin('https://stackoverflow.com', title_tag['href'])
            excerpt_tag = item.select_one('.s-post-summary--content-excerpt')
            description = excerpt_tag.text.strip() if excerpt_tag else ''
            trend = {
                'title': title,
                'description': description,
                'link': link,
                'source': 'From Stack Overflow',
                'source_class': 'StackoverflowTrending',
                'image': '/static/images/default_trendy.png',
                'video': None
            }
            trend['id'] = generate_stable_id(trend)
            results.append(trend)
        logger.debug(f"Stack Overflow: Fetched {len(results)} trends")
        return results
    except Exception as e:
        logger.error(f"Error fetching Stack Overflow trends: {e}", exc_info=True)
        return []

def get_devto_latest():
    url = 'https://dev.to/'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/112.0.0.0',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5'
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        items = soup.select('div.crayons-story')
        results = []
        for item in items[:25]:
            title_tag = item.select_one('h2.crayons-story__title a')
            if not title_tag:
                continue
            title = title_tag.text.strip()
            link = urljoin(url, title_tag['href'])
            description_tag = item.select_one('p.crayons-story__snippet')
            description = description_tag.text.strip() if description_tag else ''
            trend = {
                'title': title,
                'description': description,
                'link': link,
                'source': 'From Dev.to',
                'source_class': 'DevtoTrending',
                'image': '/static/images/default_trendy.png',
                'video': None
            }
            trend['id'] = generate_stable_id(trend)
            results.append(trend)
        logger.debug(f"Dev.to: Fetched {len(results)} trends")
        return results
    except Exception as e:
        logger.error(f"Error fetching Dev.to trends: {e}", exc_info=True)
        return []

def get_medium_technology():
    url = 'https://medium.com/tag/technology'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/112.0.0.0'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        items = soup.select('article')
        results = []
        for item in items[:25]:
            title_tag = item.select_one('h2')
            if not title_tag:
                continue
            title = title_tag.text.strip()
            link_tag = item.find('a', href=True)
            link = urljoin('https://medium.com', link_tag['href'].split('?')[0]) if link_tag else url
            description_tag = item.select_one('div[aria-hidden="true"] p')
            description = description_tag.text.strip() if description_tag else ''
            trend = {
                'title': title,
                'description': description,
                'link': link,
                'source': 'From Medium Technology',
                'source_class': 'MediumtechTrending',
                'image': '/static/images/default_trendy.png',
                'video': None
            }
            trend['id'] = generate_stable_id(trend)
            results.append(trend)
        logger.debug(f"Medium: Fetched {len(results)} trends")
        return results
    except Exception as e:
        logger.error(f"Error fetching Medium trends: {e}", exc_info=True)
        return []

def get_lobsters():
    url = 'https://lobste.rs/'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/112.0.0.0'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        items = soup.select('.story .link a')
        results = []
        for item in items[:25]:
            title = item.text.strip()
            link = item['href']
            if not link.startswith(('http://', 'https://')):
                link = urljoin(url, link)
            description = ''
            trend = {
                'title': title,
                'description': description,
                'link': link,
                'source': 'From Lobsters',
                'source_class': 'LobstersTrending',
                'image': '/static/images/default_trendy.png',
                'video': None
            }
            trend['id'] = generate_stable_id(trend)
            results.append(trend)
        logger.debug(f"Lobsters: Fetched {len(results)} trends")
        return results
    except Exception as e:
        logger.error(f"Error fetching Lobsters trends: {e}", exc_info=True)
        return []

def get_slashdot():
    url = 'https://slashdot.org/'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/112.0.0.0'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        items = soup.select('.story')
        results = []
        for item in items[:25]:
            title_tag = item.select_one('.story-title a')
            if not title_tag:
                continue
            title = title_tag.text.strip()
            link = title_tag['href']
            if not link.startswith(('http://', 'https://')):
                link = urljoin('https://slashdot.org', link)
            desc_tag = item.select_one('.p')
            description = desc_tag.text.strip() if desc_tag else ''
            trend = {
                'title': title,
                'description': description,
                'link': link,
                'source': 'From Slashdot',
                'source_class': 'SlashdotTrending',
                'image': '/static/images/default_trendy.png',
                'video': None
            }
            trend['id'] = generate_stable_id(trend)
            results.append(trend)
        logger.debug(f"Slashdot: Fetched {len(results)} trends")
        return results
    except Exception as e:
        logger.error(f"Error fetching Slashdot trends: {e}", exc_info=True)
        return []

def get_digg_popular():
    url = 'https://digg.com/'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/112.0.0.0'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        items = soup.select('article.story-item')
        results = []
        for item in items[:25]:
            title_tag = item.select_one('h2')
            if not title_tag:
                continue
            title = title_tag.text.strip()
            link_tag = item.select_one('a.story-link')
            link = link_tag['href'] if link_tag else 'https://digg.com'
            description_tag = item.select_one('.story-content p')
            description = description_tag.text.strip() if description_tag else ''
            image_tag = item.select_one('img')
            image = image_tag['src'] if image_tag and image_tag.has_attr('src') else '/static/images/default_trendy.svg'
            trend = {
                'title': title,
                'description': description,
                'link': link,
                'source': 'From Digg',
                'source_class': 'DiggTrending',
                'image': image,
                'video': None
            }
            trend['id'] = generate_stable_id(trend)
            results.append(trend)
        logger.debug(f"Digg: Fetched {len(results)} trends")
        return results
    except Exception as e:
        logger.error(f"Error fetching Digg trends: {e}", exc_info=True)
        return []

def get_bbc_trending():
    url = 'https://www.bbc.com/news'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/112.0.0.0'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        items = soup.select('div.gs-c-promo')
        results = []
        for item in items[:25]:
            title_tag = item.select_one('h3')
            if not title_tag:
                continue
            title = title_tag.text.strip()
            link_tag = item.select_one('a.gs-c-promo-heading')
            link = urljoin(url, link_tag['href']) if link_tag else url
            desc_tag = item.select_one('.gs-c-promo-summary')
            description = desc_tag.text.strip() if desc_tag else ''
            image_tag = item.select_one('img')
            image = image_tag['src'] if image_tag and image_tag.has_attr('src') else '/static/images/default_trendy.svg'
            trend = {
                'title': title,
                'description': description,
                'link': link,
                'source': 'From BBC',
                'source_class': 'BBCTrending',
                'image': image,
                'video': None
            }
            trend['id'] = generate_stable_id(trend)
            results.append(trend)
        logger.debug(f"BBC: Fetched {len(results)} trends")
        return results
    except Exception as e:
        logger.error(f"Error fetching BBC trends: {e}", exc_info=True)
        return []

def get_youtube_trending(YOUTUBE_API_KEY):
    if not YOUTUBE_API_KEY:
        logger.warning("YouTube API key not provided, skipping YouTube trends")
        return []
    url = 'https://www.googleapis.com/youtube/v3/videos'
    params = {
        'part': 'snippet',
        'chart': 'mostPopular',
        'maxResults': 25,
        'regionCode': 'US',
        'key': YOUTUBE_API_KEY
    }
    try:
        response = requests.get(url, params=params, timeout=10)
        response.raise_for_status()
        data = response.json()
        results = []
        for item in data.get('items', [])[:25]:
            snippet = item['snippet']
            video_id = item['id']
            title = snippet.get('title', '')
            description = snippet.get('description', '')
            link = f'https://www.youtube.com/watch?v={video_id}'
            image = snippet.get('thumbnails', {}).get('medium', {}).get('url', '/static/images/default_trendy.png')
            trend = {
                'title': title,
                'description': description,
                'link': link,
                'source': 'From YouTube',
                'source_class': 'YouTubeTrending',
                'image': image,
                'video': None
            }
            trend['id'] = generate_stable_id(trend)
            results.append(trend)
        logger.debug(f"YouTube: Fetched {len(results)} trends")
        return results
    except Exception as e:
        logger.error(f"Error fetching YouTube trends: {e}", exc_info=True)
        return []

def get_ars_technica():
    url = 'https://arstechnica.com/'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/112.0.0.0'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        items = soup.select('article.tease')
        results = []
        for item in items[:25]:
            title_tag = item.select_one('h2 a')
            if not title_tag:
                continue
            title = title_tag.text.strip()
            link = urljoin(url, title_tag['href'])
            desc_tag = item.select_one('p.excerpt')
            description = desc_tag.text.strip() if desc_tag else ''
            image_tag = item.select_one('img')
            image = image_tag['src'] if image_tag and image_tag.has_attr('src') else '/static/images/default_trendy.png'
            trend = {
                'title': title,
                'description': description,
                'link': link,
                'source': 'From Ars Technica',
                'source_class': 'ArsTechnicaTrending',
                'image': image,
                'video': None
            }
            trend['id'] = generate_stable_id(trend)
            results.append(trend)
        logger.debug(f"Ars Technica: Fetched {len(results)} trends")
        return results
    except Exception as e:
        logger.error(f"Error fetching Ars Technica trends: {e}", exc_info=True)
        return []

def get_wired():
    url = 'https://www.wired.com/feed/rss'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/112.0.0.0'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.content, 'xml')
        items = soup.find_all('item')
        results = []
        for item in items[:25]:
            title = item.title.text.strip()
            link = item.link.text.strip()
            description = item.description.text.strip() if item.description else ''
            trend = {
                'title': title,
                'description': description,
                'link': link,
                'source': 'From Wired',
                'source_class': 'WiredTrending',
                'image': '/static/images/default_trendy.png',
                'video': None
            }
            trend['id'] = generate_stable_id(trend)
            results.append(trend)
        logger.debug(f"Wired: Fetched {len(results)} trends")
        return results
    except Exception as e:
        logger.error(f"Error fetching Wired trends: {e}", exc_info=True)
        return []

def get_goodreads_trending():
    url = 'https://www.goodreads.com/book/popular_by_date/2025'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/112.0.0.0'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        book_sections = soup.select('div.tableList tr')
        results = []
        for row in book_sections[:25]:
            link_tag = row.select_one('a.bookTitle')
            image_tag = row.select_one('img.bookCover')
            if not link_tag or not image_tag:
                continue
            title = link_tag.text.strip()
            link = 'https://www.goodreads.com' + link_tag['href']
            image = image_tag['src']
            trend = {
                'title': title,
                'description': '',
                'image': image,
                'link': link,
                'source': 'From Goodreads',
                'source_class': 'GoodreadsTrending',
                'video': None
            }
            trend['id'] = generate_stable_id(trend)
            results.append(trend)
        logger.debug(f"Goodreads: Fetched {len(results)} trends")
        return results
    except Exception as e:
        logger.error(f"Error fetching Goodreads trends: {e}", exc_info=True)
        return []

def get_steam_charts():
    url = 'https://steamcharts.com/top'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/112.0.0.0'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        rows = soup.select('table.common-table tbody tr')
        results = []
        for row in rows[:25]:
            name_tag = row.select_one('td.game-name > a')
            if not name_tag:
                continue
            title = name_tag.text.strip()
            link = 'https://steamcharts.com' + name_tag['href']
            app_id = name_tag['href'].split('/')[-1]
            image = f'https://cdn.cloudflare.steamstatic.com/steam/apps/{app_id}/header.jpg'
            trend = {
                'title': title,
                'description': '',
                'image': image,
                'link': link,
                'source': 'From Steam Charts',
                'source_class': 'SteamChartsTrending',
                'video': None
            }
            trend['id'] = generate_stable_id(trend)
            results.append(trend)
        logger.debug(f"Steam Charts: Fetched {len(results)} trends")
        return results
    except Exception as e:
        logger.error(f"Error fetching Steam Charts trends: {e}", exc_info=True)
        return []

def get_spotify_charts(client_id, client_secret):
    if not client_id or not client_secret:
        logger.warning("Spotify Client ID or Client Secret not provided, skipping Spotify trends")
        return []
    try:
        # Authenticate with Spotify APIs
        
        # Fetch Global Top 50 playlist
        playlist_id = '37i9dQZEVXbMDoHDwVN2tF'  # Spotify Global Top 50
        playlist = sp.playlist(playlist_id)
        results = []
        for item in playlist['tracks']['items'][:25]:
            track = item['track']
            title = track['name']
            artist = ', '.join(artist['name'] for artist in track['artists'])
            description = f'{title} by {artist}'
            link = track['external_urls']['spotify']
            image = track['album']['images'][0]['url'] if track['album']['images'] else '/static/images/default_trendy.png'
            trend = {
                'title': title,
                'description': description,
                'link': link,
                'source': 'From Spotify Charts',
                'source_class': 'SpotifyTrending',
                'image': image,
                'video': None
            }
            trend['id'] = generate_stable_id(trend)
            results.append(trend)
        logger.debug(f"Spotify Charts: Fetched {len(results)} trends")
        return results
    except Exception as e:
        logger.error(f"Error fetching Spotify Charts trends: {e}", exc_info=True)
        return []

def get_billboard_trending():
    url = 'https://www.billboard.com/charts/hot-100'
    headers = {'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/112.0.0.0'}
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        items = soup.select('li.o-chart-results-list__item h3')
        results = []
        for i, h3 in enumerate(items[:25]):
            title = h3.get_text(strip=True)
            artist_tag = h3.find_next('span')
            artist = artist_tag.get_text(strip=True) if artist_tag else 'Unknown Artist'
            trend = {
                'title': f'{title} — {artist}',
                'description': f'#{i+1} on Billboard Hot 100',
                'link': url,
                'source': 'From Billboard',
                'source_class': 'BillboardTrending',
                'image': '/static/images/default_trendy.png',
                'video': None
            }
            trend['id'] = generate_stable_id(trend)
            results.append(trend)
        logger.debug(f"Billboard: Fetched {len(results)} trends")
        return results
    except Exception as e:
        logger.error(f"Error fetching Billboard trends: {e}", exc_info=True)
        return []

def get_imdb_trending():
    url = 'https://www.imdb.com/chart/moviemeter/'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/112.0.0.0',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5'
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        rows = soup.select('ul.ipc-metadata-list li.ipc-metadata-list-summary-item')
        results = []
        for i, row in enumerate(rows[:25]):
            title_column = row.select_one('a.ipc-title-link-wrapper')
            if not title_column:
                continue
            title = title_column.text.strip().split('. ', 1)[-1]
            link = 'https://www.imdb.com' + title_column['href'].split('?')[0]
            year_span = row.select_one('span.sc-b189961a-8')
            year = year_span.text.strip() if year_span else ''
            description = f'Rank #{i+1} trending on IMDb Moviemeter {year}'
            image_tag = row.select_one('img.ipc-image')
            image = image_tag['src'] if image_tag and image_tag.has_attr('src') else '/static/images/default_trendy.png'
            trend = {
                'title': title,
                'description': description,
                'link': link,
                'source': 'From IMDb',
                'source_class': 'IMDbTrending',
                'image': image,
                'video': None
            }
            trend['id'] = generate_stable_id(trend)
            results.append(trend)
        logger.debug(f"IMDb: Fetched {len(results)} trends")
        return results
    except Exception as e:
        logger.error(f"Error fetching IMDb trends: {e}", exc_info=True)
        return []

def get_cnn_trending():
    url = 'https://www.cnn.com/world'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) Chrome/112.0.0.0',
        'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8',
        'Accept-Language': 'en-US,en;q=0.5'
    }
    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        articles = soup.select('a.container__link--type-article')
        results = []
        for i, article in enumerate(articles[:25]):
            title = article.get_text(strip=True)
            if not title:
                continue
            link = article['href']
            if not link.startswith('http'):
                link = f'https://www.cnn.com{link}'
            description = f'#{i+1} on CNN World'
            image_tag = article.find_parent().select_one('img')
            image = image_tag['src'] if image_tag and image_tag.has_attr('src') else '/static/images/default_trendy.png'
            trend = {
                'title': title,
                'description': description,
                'link': link,
                'source': 'From CNN',
                'source_class': 'CNNTrending',
                'image': image,
                'video': None
            }
            trend['id'] = generate_stable_id(trend)
            results.append(trend)
        logger.debug(f"CNN: Fetched {len(results)} trends")
        return results
    except Exception as e:
        logger.error(f"Error fetching CNN trends: {e}", exc_info=True)
        return []
    
def fetch_reuters_trending():
    url = "https://www.reuters.com/"  # Changed to main page due to 401 error on /world/
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://www.reuters.com/"
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        # Updated selector based on Reuters homepage structure (may need adjustment)
        articles = soup.select("a[data-testid='Heading']")  # Adjust selector after inspecting page
        results = []
        for i, article in enumerate(articles[:10]):
            title = article.get_text(strip=True)
            link = article['href']
            if not link.startswith("http"):
                link = f"https://www.reuters.com{link}"

            trend = {
                "id": str(uuid.uuid4()),  # Temporary ID
                "title": title,
                "description": f"#{i + 1} on Reuters",
                "link": link,
                "source": "From Reuters",
                "source_class": "ReutersTrending",
                "image": "/static/images/default_trendy.png",
                "video": None
            }
            trend['id'] = generate_stable_id(trend)  # Assign stable ID inside loop
            results.append(trend)  # Append inside loop

        return results

    except Exception as e:
        print(f"Error fetching Reuters trending: {e}")
        return []
# ---------------------------- AGGREGATE AND CACHE ---------------------------- #

def fetch_all_trends():
    global global_trends, last_fetch_time
    logger.debug("Starting fetch_all_trends")
    all_trends = []
    seen_ids = set()
    funcs = [
        get_hacker_news,
        get_github_trending,
        get_medium_technology,
        get_stackoverflow_trending,
        get_devto_latest,
        get_lobsters,
        get_slashdot,
        get_wired,
        get_steam_charts,
        get_billboard_trending,
        get_imdb_trending,
        get_cnn_trending
    ]
    try:
        existing_trends = {trend.id: trend for trend in Trend.query.all()}
        logger.debug(f"Loaded {len(existing_trends)} existing trends")
    except Exception as e:
        logger.error(f"Error querying trends: {e}", exc_info=True)
        existing_trends = {}

    for func in funcs:
        try:
            trends = func()
            if not trends or not isinstance(trends, list):
                logger.warning(f"Source {func.__name__} returned invalid: {type(trends)}")
                continue
            all_trends.extend(trends)
            logger.debug(f"Fetched {len(trends)} from {func.__name__}")
        except Exception as e:
            logger.error(f"Error in {func.__name__}: {e}", exc_info=True)

    new_global_trends = []
    now = datetime.now(timezone.utc)
    for trend in all_trends:
        trend_id = generate_stable_id(trend)
        if trend_id in seen_ids:
            logger.warning(f"Skipping duplicate ID {trend_id}: {trend.get('title', 'Unknown')} (source: {trend.get('source', 'Unknown')})")
            continue
        seen_ids.add(trend_id)
        trend['id'] = trend_id
        existing_trend = existing_trends.get(trend_id)
        if existing_trend:
            logger.debug(f"Existing trend {trend_id}: {trend.get('title', 'Untitled')}, timestamp: {existing_trend.timestamp}")
            timestamp = existing_trend.timestamp
            if timestamp.tzinfo is None:
                timestamp = timestamp.replace(tzinfo=timezone.utc)
            trend['timestamp'] = timestamp.isoformat()
            new_global_trends.append(trend)
        else:
            logger.debug(f"New trend {trend_id}: {trend.get('title', 'Untitled')}")
            new_trend = Trend(
                id=trend_id,
                title=trend.get('title', 'Untitled'),
                image=trend.get('image', '/static/images/default_trendy.png'),
                description=trend.get('description', ''),
                link=trend.get('link', ''),
                source=trend.get('source', 'Unknown'),
                timestamp=now
            )
            db.session.add(new_trend)
            trend['timestamp'] = now.isoformat()
            new_global_trends.append(trend)

    try:
        db.session.commit()
        logger.debug("Database commit successful")
    except Exception as e:
        logger.error(f"Error committing database: {e}", exc_info=True)
        db.session.rollback()

    try:
        cleanup_old_trends()
    except Exception as e:
        logger.error(f"Error cleaning trends: {e}", exc_info=True)

    try:
        new_global_trends.sort(
            key=lambda x: datetime.fromisoformat(x['timestamp']).astimezone(timezone.utc),
            reverse=True
        )
    except Exception as e:
        logger.error(f"Error sorting trends: {e}", exc_info=True)
        new_global_trends.sort(key=lambda x: x['timestamp'], reverse=True)

    global_trends = new_global_trends[:2000]
    last_fetch_time = now
    logger.debug(f"Total trends: {len(global_trends)}")
    return global_trends

def background_fetch():
    logger.debug("Background fetch started")
    while True:
        try:
            with app.app_context():
                fetch_all_trends()
            logger.debug("Background fetch completed")
        except Exception as e:
            logger.error(f"Background fetch error: {e}", exc_info=True)
        time.sleep(600)

if os.getenv('RENDER'):
    threading.Thread(target=background_fetch, daemon=True).start()
    logger.debug("Background fetch thread started")

@app.route('/')
def home():
    global global_trends, last_fetch_time
    logger.debug("Rendering home")
    now = datetime.now(timezone.utc)
    if not global_trends or (last_fetch_time and (now - last_fetch_time).total_seconds() > 300):
        logger.debug("Fetching trends: empty or stale")
        try:
            with app.app_context():
                trends = Trend.query.order_by(Trend.timestamp.desc()).limit(2000).all()
            logger.debug(f"Loaded {len(trends)} trends from database")
            global_trends = [{
                'id': t.id,
                'title': t.title,
                'image': t.image,
                'description': t.description,
                'link': t.link,
                'source': t.source,
                'source_class': t.source,
                'timestamp': t.timestamp.isoformat() if t.timestamp.tzinfo else t.timestamp.replace(tzinfo=timezone.utc).isoformat(),
                'video': None
            } for t in trends]
            last_fetch_time = now
        except Exception as e:
            logger.error(f"Error loading trends: {e}", exc_info=True)
            with app.app_context():
                fetch_all_trends()
    else:
        logger.debug("Using cached trends")

    trends = global_trends
    if not trends:
        logger.warning("No trends available")
        trends = []

    random.shuffle(trends)
    trend_of_the_day = get_trend_of_the_day(trends)
    unique_sources = sorted(set(trend['source'] for trend in trends))
    vote_counts = db.session.query(
        Vote.trend_id,
        Vote.vote_type,
        db.func.count().label('count')
    ).group_by(Vote.trend_id, Vote.vote_type).all()
    vote_counts_dict = {}
    for v in vote_counts:
        if v.trend_id not in vote_counts_dict:
            vote_counts_dict[v.trend_id] = {}
        vote_counts_dict[v.trend_id][v.vote_type] = v.count
    logger.debug(f"Rendering {len(trends)} trends, sources: {unique_sources}")
    return render_template(
        'index.html',
        trends=trends,
        trend_of_the_day=trend_of_the_day,
        unique_sources=unique_sources,
        vote_counts=vote_counts_dict
    )

@app.route('/chat')
def chat():
    return render_template('chat.html')

@app.route('/api/trends')
def api_trends():
    logger.debug("Serving /api/trends")
    return jsonify(global_trends[:2000])

@app.route('/trend/<trend_id>')
def trend_detail(trend_id):
    logger.debug(f"Rendering trend {trend_id}")
    trend = next((t for t in global_trends if t['id'] == trend_id), None)
    if not trend:
        logger.warning(f"Trend not found: {trend_id}")
        try:
            return render_template('404.html'), 404
        except Exception as e:
            logger.error(f"404.html template not found: {e}", exc_info=True)
            response = make_response("Trend not found", 404)
            response.headers['Content-Type'] = 'text/plain'
            return response
    summary = generate_summary(trend)
    vote_counts = db.session.query(
        Vote.vote_type,
        db.func.count().label('count')
    ).filter_by(trend_id=trend_id).group_by(Vote.vote_type).all()
    vote_counts_dict = {v.vote_type: v.count for v in vote_counts}
    return render_template(
        'trend_detail.html',
        trend=trend,
        summary=summary,
        vote_counts=vote_counts_dict
    )

@app.route('/api/vote', methods=['POST'])
def vote():
    logger.debug("Processing vote")
    data = request.json
    trend_id = data.get('trend_id')
    vote_type = data.get('vote_type')
    ip = request.remote_addr
    if not trend_id or not vote_type:
        logger.error(f"Missing vote data: {data}")
        return jsonify({'error': 'Missing trend_id or vote_type'}), 400
    existing_vote = Vote.query.filter_by(trend_id=trend_id, ip_address=ip).first()
    if existing_vote:
        logger.warning(f"Duplicate vote from {ip} for {trend_id}")
        return jsonify({'error': 'Already voted'}), 403
    vote = Vote(trend_id=trend_id, ip_address=ip, vote_type=vote_type)
    db.session.add(vote)
    try:
        db.session.commit()
        logger.debug(f"Vote recorded: {trend_id}, {vote_type}")
    except Exception as e:
        logger.error(f"Error committing vote: {e}", exc_info=True)
        db.session.rollback()
        return jsonify({'error': 'Database error'}), 500
    vote_counts = db.session.query(
        Vote.vote_type,
        db.func.count().label('count')
    ).filter_by(trend_id=trend_id).group_by(Vote.vote_type).all()
    return jsonify({v.vote_type: v.count for v in vote_counts})

@app.route('/fetch-trends')
def fetch_trends():
    try:
        with app.app_context():
            fetch_all_trends()
        logger.debug("Manual fetch completed")
        return jsonify({"status": "success", "trend_count": len(global_trends)})
    except Exception as e:
        logger.error(f"Manual fetch failed: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/test-vote')
def test_vote():
    try:
        test_trend_id = hashlib.md5("test_trend".encode()).hexdigest()
        trend = Trend.query.get(test_trend_id)
        if not trend:
            trend = Trend(
                id=test_trend_id,
                title="Test Trend",
                description="Test trend for voting",
                source="Test",
                timestamp=datetime.now(timezone.utc)
            )
            db.session.add(trend)
            db.session.commit()
            logger.debug("Test trend inserted")
        test_ip = "127.0.0.1"
        vote = Vote.query.filter_by(trend_id=test_trend_id, ip_address=test_ip).first()
        if not vote:
            vote = Vote(
                test_trend_id,
                ip_address=test_ip,
                vote_type="upvote",
                timestamp=datetime.now(timezone.utc)
            )
            db.session.add(vote)
            db.session.commit()
            logger.debug("Test vote inserted")
        vote_count = Vote.query.count()
        logger.debug(f"Vote count: {vote_count}")
        return jsonify({"status": "success", "vote_count": vote_count})
    except Exception as e:
        logger.error(f"Test vote failed: {e}", exc_info=True)
        db.session.rollback()
        return jsonify({"status": "error", "message": str(e)}), 500

@app.route('/debug-request')
def debug_request():
    logger.debug(f"Request: {request.host}, {request.url}")
    return jsonify({"host": request.host, "url": request.url})

@app.route('/debug-rooms')
def debug_rooms():
    try:
        all_rooms = {}
        for sid in socketio.server.manager.get_participants('/', None):
            client_rooms = rooms(sid=sid, namespace='/')
            all_rooms[sid] = client_rooms
        logger.debug(f"Active rooms: {all_rooms}")
        return jsonify({"status": "success", "rooms": all_rooms})
    except Exception as e:
        logger.error(f"Error fetching rooms: {e}", exc_info=True)
        return jsonify({"status": "error", "message": str(e)}), 500

@socketio.on('connect')
def handle_connect():
    logger.debug(f"Client connected: {request.sid}")

@socketio.on('disconnect')
def handle_disconnect():
    logger.debug(f"Client disconnected: {request.sid}")

@socketio.on('join')
def handle_join(data):
    room = data.get('room')
    username = data.get('username', 'Guest')
    if not room or not isinstance(room, str):
        logger.error(f"Invalid room in join: {room}")
        emit('error', {'message': 'Invalid room'}, to=request.sid)
        return
    join_room(room)
    current_rooms = rooms(sid=request.sid)
    logger.debug(f"User {username} (SID: {request.sid}) joined room {room}. Current rooms: {current_rooms}")
    emit('message', f"{username} entered chat.", room=room, include_self=True)

@socketio.on('leave')
def handle_leave(data):
    room = data.get('room')
    username = data.get('username', 'Guest')
    if not room or not isinstance(room, str):
        logger.error(f"Invalid room in leave: {room}")
        emit('error', {'message': 'Invalid room'}, to=request.sid)
        return
    leave_room(room)
    logger.debug(f"User {username} (SID: {request.sid}) left room {room}")
    emit('message', f"{username} left chat.", room=room, include_self=False)

@socketio.on('message')
def handle_message(data):
    room = data.get('room')
    username = data.get('username', 'Guest')
    message = data.get('message', '').strip()
    if not room or not isinstance(room, str):
        logger.error(f"Invalid room in message: {room}")
        emit('error', {'message': 'Invalid room'}, to=request.sid)
        return
    if not message or len(message) > 200:
        logger.warning(f"Invalid message from {username} in room {room}: {message}")
        emit('error', {'message': 'Message empty or too long'}, to=request.sid)
        return
    msg = f"{username}: {message}"
    logger.debug(f"Broadcasting message: {msg} in room {room} (SID: {request.sid})")
    emit('message', msg, room=room, include_self=True)

if __name__ == '__main__':
    logger.info("Starting local app")
    with app.app_context():
        fetch_all_trends()
    socketio.run(app, host='127.0.0.1', port=5000, debug=True, allow_unsafe_werkzeug=True)