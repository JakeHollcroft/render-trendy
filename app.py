from flask import Flask, render_template, request, jsonify
from flask_sqlalchemy import SQLAlchemy
from transformers import pipeline
import os
import hashlib
import random
import threading
import time
import logging
from datetime import datetime, timezone
import re
from collections import Counter
from bs4 import BeautifulSoup
import requests
from urllib.parse import urljoin

app = Flask(__name__)

logging.basicConfig(level=logging.DEBUG)
logger = logging.getLogger(__name__)

trends = []
YOUTUBE_API_KEY = os.getenv('YOUTUBE_API_KEY')

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:////opt/render/data/trendy.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db = SQLAlchemy(app)

class Trend(db.Model):
    id = db.Column(db.String, primary_key=True)
    title = db.Column(db.String, nullable=False)
    image = db.Column(db.String)
    description = db.Column(db.Text)
    link = db.Column(db.String)
    source = db.Column(db.String)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

class Vote(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    trend_id = db.Column(db.String, db.ForeignKey('trend.id'), nullable=False)
    ip_address = db.Column(db.String, nullable=False)
    vote_type = db.Column(db.String, nullable=False)
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)
    __table_args__ = (
        db.UniqueConstraint('trend_id', 'ip_address', name='unique_vote_per_ip'),
    )

with app.app_context():
    db.create_all()

t5_summarizer = pipeline("summarization", model="t5-small")

STOP_WORDS = {
    'with', 'from', 'this', 'that', 'have', 'will', 'more', 'some', 'what', 'when',
    'where', 'which', 'into', 'over', 'under', 'about', 'there', 'their', 'they',
    'were', 'been', 'being', 'than', 'then', 'once', 'here', 'after', 'before',
    'during', 'while', 'because', 'since', 'until', 'again', 'against', 'between'
}

def generate_stable_id(trend):
    key = (trend.get("title", "") + trend.get("link", "")).strip()
    return hashlib.md5(key.encode("utf-8")).hexdigest()

def time_ago(timestamp_str):
    past = datetime.fromisoformat(timestamp_str).replace(tzinfo=timezone.utc)
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
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

app.jinja_env.globals.update(time_ago=time_ago)

def get_hacker_news():
    url = 'https://news.ycombinator.com/'
    soup = BeautifulSoup(requests.get(url).text, 'html.parser')
    items = soup.select('.athing')
    results = []
    for item in items[:5]:
        title_tag = item.select_one('.titleline')
        if not title_tag:
            continue
        title = title_tag.text
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
            'image': '/static/images/default_trendy.svg',
            'video': None,
            'timestamp': datetime.utcnow().isoformat()
        }

        trend['id'] = generate_stable_id(trend)

        results.append(trend)

    return results

def get_github_trending():
    url = 'https://github.com/trending'
    soup = BeautifulSoup(requests.get(url).text, 'html.parser')
    items = soup.select('article.Box-row')
    results = []
    for item in items[:5]:
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
            'image': '/static/images/default_trendy.svg',
            'video': None,
            'timestamp': datetime.utcnow().isoformat()
        }
        trend['id'] = generate_stable_id(trend)

        results.append(trend)
    return results

def get_reddit_top():
    headers = {'User-agent': 'TrendyScraper 1.0'}
    url = 'https://www.reddit.com/r/popular/top.json?limit=10'
    response = requests.get(url, headers=headers)
    if response.status_code != 200:
        return []
    results = []
    json_data = response.json()
    for item in json_data.get('data', {}).get('children', []):
        data = item['data']
        reddit_url = 'https://reddit.com' + data.get('permalink', '')
        title = data.get('title', 'No title')
        description = data.get('selftext', '')  # Reddit post body as description

        video_url = None
        image_url = None

        media = data.get('secure_media') or data.get('media')
        if media and 'reddit_video' in media:
            reddit_video = media['reddit_video']
            video_url = reddit_video.get('fallback_url')

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
            'image': image_url if image_url else '/static/images/default_trendy.svg',
            'video': video_url,
            'timestamp': datetime.utcnow().isoformat()
        }
        trend['id'] = generate_stable_id(trend)

        results.append(trend)
    return results


def get_techcrunch():
    url = 'https://techcrunch.com/'
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/112.0.0.0 Safari/537.36'
    }
    response = requests.get(url, headers=headers)
    soup = BeautifulSoup(response.text, 'html.parser')

    items = soup.select('a.post-block__title__link')
    results = []

    for item in items[:5]:
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
            'video': None,
            'timestamp': datetime.utcnow().isoformat()
        }
        trend['id'] = generate_stable_id(trend)

        results.append(trend)
    return results

def get_stackoverflow_trending():
    url = 'https://stackoverflow.com/questions?tab=Hot'
    soup = BeautifulSoup(requests.get(url).text, 'html.parser')
    items = soup.select('.question-summary')
    results = []
    for item in items[:5]:
        title_tag = item.select_one('.question-hyperlink')
        if not title_tag:
            continue
        title = title_tag.text
        link = 'https://stackoverflow.com' + title_tag['href']

        # Stack Overflow questions have excerpts
        excerpt_tag = item.select_one('.excerpt')
        description = excerpt_tag.text.strip() if excerpt_tag else ''

        trend = {
            'title': title,
            'description': description,
            'link': link,
            'source': 'From Stack Overflow',
            'source_class': 'StackoverflowTrending',
            'image': '/static/images/default_trendy.svg',
            'video': None,
            'timestamp': datetime.utcnow().isoformat()
        }
        trend['id'] = generate_stable_id(trend)

        results.append(trend)
    return results

def get_devto_latest():
    url = 'https://dev.to/'
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5"
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, 'html.parser')
        items = soup.select('div.crayons-story')
        results = []

        for item in items[:5]:
            title_tag = item.select_one('h2.crayons-story__title a')
            description_tag = item.select_one('div.crayons-story__body')
            if title_tag:
                title = title_tag.text.strip()
                link = title_tag['href']
                if not link.startswith('http'):
                    link = urljoin(url, link)
                description = description_tag.text.strip() if description_tag else ''

                trend = {
                    'id': str(uuid.uuid4()),  # Temporary ID
                    'title': title,
                    'description': description,
                    'link': link,
                    'source': 'From Dev.to',
                    'source_class': 'DevtoTrending',
                    'image': '/static/images/default_trendy.svg',
                    'video': None,
                    'timestamp': datetime.utcnow().isoformat()
                }
                trend['id'] = generate_stable_id(trend)  # Assign stable ID inside loop
                results.append(trend)  # Append inside loop
        return results

    except Exception as e:
        print(f"Error fetching Dev.to trending: {e}")
        return []
    
def get_medium_technology():
    url = 'https://medium.com/topic/technology'
    headers = {'User-Agent': 'Mozilla/5.0'}
    soup = BeautifulSoup(requests.get(url, headers=headers).text, 'html.parser')
    items = soup.select('div.postArticle-content')
    results = []
    for item in items[:5]:
        title_tag = item.select_one('h3')
        subtitle_tag = item.select_one('h4')
        title = title_tag.text.strip() if title_tag else ''
        description = subtitle_tag.text.strip() if subtitle_tag else ''
        # Medium links can be tricky, fallback to main topic page
        link_tag = item.find_parent('a')
        link = link_tag['href'] if link_tag and link_tag.has_attr('href') else url
        trend = {
            'title': title,
            'description': description,
            'link': link,
            'source': 'From Medium Technology',
            'source_class': 'MediumtechTrending',
            'image': '/static/images/default_trendy.svg',
            'video': None,
            'timestamp': datetime.utcnow().isoformat()
        }
        trend['id'] = generate_stable_id(trend)

        results.append(trend)
    return results

def get_lobsters():
    url = 'https://lobste.rs/'
    soup = BeautifulSoup(requests.get(url).text, 'html.parser')
    items = soup.select('.story-link')
    results = []
    for item in items[:5]:
        title = item.text.strip()
        link = item['href']
        # Lobsters doesn't provide descriptions on homepage
        description = ''
        trend = {
            'title': title,
            'description': description,
            'link': link,
            'source': 'From Lobsters',
            'source_class': 'LobstersTrending',
            'image': '/static/images/default_trendy.svg',
            'video': None,
            'timestamp': datetime.utcnow().isoformat()
        }
        trend['id'] = generate_stable_id(trend)

        results.append(trend)
    return results

def get_slashdot():
    url = 'https://slashdot.org/'
    soup = BeautifulSoup(requests.get(url).text, 'html.parser')
    items = soup.select('.story')
    results = []
    for item in items[:5]:
        title_tag = item.select_one('.story-title a')
        desc_tag = item.select_one('.story-summary')
        if not title_tag:
            continue
        title = title_tag.text.strip()
        link = title_tag['href']
        description = desc_tag.text.strip() if desc_tag else ''
        trend = {
            'title': title,
            'description': description,
            'link': link,
            'source': 'From Slashdot',
            'source_class': 'SlashdotTrending',
            'image': '/static/images/default_trendy.svg',
            'video': None,
            'timestamp': datetime.utcnow().isoformat()
        }
        trend['id'] = generate_stable_id(trend)

        results.append(trend)
    return results

def get_digg_popular():
    url = 'https://digg.com/api/news/popular'
    response = requests.get(url)
    if response.status_code != 200:
        return []
    data = response.json()
    results = []
    for item in data.get('data', {}).get('feed', [])[:5]:
        title = item.get('content', {}).get('title', '')
        description = item.get('content', {}).get('description', '')
        link = item.get('content', {}).get('url', '')
        image = None
        media = item.get('content', {}).get('media', [])
        if media:
            image = media[0].get('original_url')
        trend = {
            'title': title,
            'description': description,
            'link': link,
            'source': 'From Digg',
            'source_class': 'DiggTrending',
            'image': image if image else '/static/images/default_trendy.svg',
            'video': None,
            'timestamp': datetime.utcnow().isoformat()
        }
        trend['id'] = generate_stable_id(trend)

        results.append(trend)
    return results

def get_bbc_trending():
    url = 'https://www.bbc.com/news'
    soup = BeautifulSoup(requests.get(url).text, 'html.parser')
    items = soup.select('a.gs-c-promo-heading')
    results = []
    for item in items[:5]:
        title = item.text.strip()
        link = item['href']
        if not link.startswith('http'):
            link = urljoin(url, link)
        # BBC summaries often in sibling div with class promo-summary or similar
        parent = item.parent
        description = ''
        if parent:
            desc_tag = parent.select_one('.gs-c-promo-summary')
            if desc_tag:
                description = desc_tag.text.strip()
        trend = {
            'title': title,
            'description': description,
            'link': link,
            'source': 'From BBC',
            'source_class': 'BBCTrending',
            'image': '/static/images/default_trendy.svg',
            'video': None,
            'timestamp': datetime.utcnow().isoformat()
        }
        trend['id'] = generate_stable_id(trend)

        results.append(trend)
    return results

def get_twitter_trending():
    # Unofficial and fragile, scrapes recent #technology tweets from Twitter search
    url = 'https://twitter.com/search?q=%23technology&f=live'
    headers = {
        'User-Agent': 'Mozilla/5.0',
    }
    soup = BeautifulSoup(requests.get(url, headers=headers).text, 'html.parser')
    tweets = soup.select('article div[lang]')
    results = []
    for tweet in tweets[:5]:
        text = tweet.text.strip()
        # Twitter no direct links to tweet in scraping without JS, so just hashtag search link
        trend = {
            'title': text[:50] + ('...' if len(text) > 50 else ''),
            'description': text,
            'link': 'https://twitter.com/search?q=%23technology',
            'source': 'From X',
            'source_class': 'XTrending',
            'image': '/static/images/default_trendy.svg',
            'video': None,
            'timestamp': datetime.utcnow().isoformat()
        }
        trend['id'] = generate_stable_id(trend)

        results.append(trend)
    return results

def get_youtube_trending(YOUTUBE_API_KEY):
    url = "https://www.googleapis.com/youtube/v3/videos"
    params = {
        "part": "snippet",
        "chart": "mostPopular",
        "maxResults": 25,
        "regionCode": "US",  # Adjust region if you want
        "key": YOUTUBE_API_KEY
    }
    
    response = requests.get(url, params=params)
    data = response.json()
    
    results = []
    for item in data.get("items", []):
        snippet = item["snippet"]
        video_id = item["id"]
        title = snippet.get("title", "")
        description = snippet.get("description", "")
        link = f"https://www.youtube.com/watch?v={video_id}"
        image = snippet.get("thumbnails", {}).get("medium", {}).get("url", "/static/images/default_trendy.svg")
        
        trend = {
            'title': title,
            'description': description,
            'link': link,
            'source': 'From YouTube',
            'source_class': 'YouTubeTrending',
            'image': image,
            'video': None,
            'timestamp': datetime.utcnow().isoformat()
        }
        trend['id'] = generate_stable_id(trend)

        results.append(trend)
    return results

def get_ars_technica():
    url = 'https://feeds.arstechnica.com/arstechnica/index'
    response = requests.get(url)
    soup = BeautifulSoup(response.content, 'xml')
    items = soup.find_all('item')
    results = []
    for item in items[:5]:
        title = item.title.text.strip()
        link = item.link.text.strip()
        description = item.description.text.strip()

        trend = {
            'title': title,
            'description': description,
            'link': link,
            'source': 'From Ars Technica',
            'source_class': 'ArsTechnicaTrending',
            'image': '/static/images/default_trendy.svg',
            'video': None,
            'timestamp': datetime.utcnow().isoformat()
        }
        trend['id'] = generate_stable_id(trend)

        results.append(trend)
    return results

def get_wired():
    url = 'https://www.wired.com/feed/rss'
    response = requests.get(url)
    soup = BeautifulSoup(response.content, 'xml')
    items = soup.find_all('item')
    results = []
    for item in items[:5]:
        title = item.title.text.strip()
        link = item.link.text.strip()
        description = item.description.text.strip()

        trend = {
            'title': title,
            'description': description,
            'link': link,
            'source': 'From Wired',
            'source_class': 'WiredTrending',
            'image': '/static/images/default_trendy.svg',
            'video': None,
            'timestamp': datetime.utcnow().isoformat()
        }
        trend['id'] = generate_stable_id(trend)

        results.append(trend)
    return results

def fetch_goodreads_trending():
    url = "https://www.goodreads.com/book/popular_by_date/2025"
    headers = {
        "User-Agent": "Mozilla/5.0"
    }
    response = requests.get(url, headers=headers)
    soup = BeautifulSoup(response.text, "html.parser")

    results = []

    book_sections = soup.select("div.tableList tr")[:20]  # Top 20 books

    for row in book_sections:
        link_tag = row.select_one("a.bookTitle")
        image_tag = row.select_one("img.bookCover")

        if not link_tag or not image_tag:
            continue

        title = link_tag.text.strip()
        link = "https://www.goodreads.com" + link_tag['href']
        image = image_tag['src']

        trend = {
            "title": title,
            "description": None,
            "image": image,
            "link": link,
            "source": "Goodreads",
            "source_class": "GoodreadsTrending"
        }

        trend['id'] = generate_stable_id(trend)

        results.append(trend)

    return results

def fetch_steam_charts():
    url = "https://steamcharts.com/top"
    headers = {
        "User-Agent": "Mozilla/5.0"
    }
    response = requests.get(url, headers=headers)
    soup = BeautifulSoup(response.text, "html.parser")

    results = []
    timestamp = datetime.utcnow().isoformat()

    rows = soup.select("table.common-table tbody tr")[:50]  # Top 20 games

    for row in rows:
        name_tag = row.select_one("td.game-name > a")
        if not name_tag:
            continue

        title = name_tag.text.strip()
        link = "https://steamcharts.com" + name_tag['href']
        app_id = name_tag['href'].split('/')[-1]
        image = f"https://cdn.cloudflare.steamstatic.com/steam/apps/{app_id}/header.jpg"

        trend = {
            "title": title,
            "description": None,
            "image": image,
            "link": link,
            "source": "From Steam Charts",
            "source_class": "SteamChartsTrending",
            "timestamp": timestamp
        }
        trend['id'] = generate_stable_id(trend)

        results.append(trend)

    return results

def scrape_spotify_charts():
    url = 'https://spotifycharts.com/regional/global/daily/latest'
    response = requests.get(url)
    soup = BeautifulSoup(response.text, 'html.parser')

    rows = soup.select('table.chart-table tbody tr')

    results = []

    for row in rows:
        title = row.select_one('.chart-table-track strong').text.strip()
        artist = row.select_one('.chart-table-track span').text.strip().replace('by ', '')
        description = f"{title} by {artist}"
        link_tag = row.select_one('.chart-table-image a')
        link = link_tag['href'] if link_tag else url
        image_style = row.select_one('.chart-table-image')['style']
        image_url = image_style.split("url('")[1].split("')")[0] if "url('" in image_style else ""

        trend = {
            'title': title,
            'image': image_url,
            'description': description,
            'link': link,
            'source': 'Spotify Charts',
            "source_class": "SpotifyTrending",
            'timestamp': datetime.utcnow().isoformat()
        }
        trend['id'] = generate_stable_id(trend)

        results.append(trend)

    return results

def get_billboard_trending():
    url = "https://www.billboard.com/charts/hot-100"
    headers = {
        "User-Agent": "Mozilla/5.0"
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        results = []

        chart_items = soup.select("li.o-chart-results-list__item > h3")

        for i, h3 in enumerate(chart_items[:50]):  # Limit to top 10
            title = h3.get_text(strip=True)
            artist_tag = h3.find_next("span")  # usually the artist is nearby
            artist = artist_tag.get_text(strip=True) if artist_tag else "Unknown Artist"

            trend = {
                'title': f"{title} — {artist}",
                'description': f"#{i+1} on Billboard Hot 100",
                'link': url,
                'source': 'From Billboard',
                'source_class': 'BillboardTrending',
                'image': "/static/images/default_trendy.svg",
                'video': None,
                'timestamp': datetime.utcnow().isoformat()
            }
        trend['id'] = generate_stable_id(trend)

        results.append(trend)

        return results

    except Exception as e:
        print("Error fetching Billboard trending:", e)
        return []
    
def get_imdb_trending():
    url = "https://www.imdb.com/chart/moviemeter/"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://www.imdb.com/"
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        results = []
        # Updated selector based on IMDb's current structure (as of May 2025)
        rows = soup.select("ul.ipc-metadata-list li.ipc-metadata-list-summary-item")
        logger.debug(f"IMDb: Found {len(rows)} rows with selector 'ul.ipc-metadata-list li.ipc-metadata-list-summary-item'")

        if not rows:
            # Log a snippet of the HTML for debugging
            logger.debug(f"IMDb: HTML snippet: {soup.prettify()[:1000]}")

        for i, row in enumerate(rows[:10]):
            title_column = row.find("a", class_="ipc-title-link-wrapper")
            if not title_column:
                logger.debug(f"IMDb: No title column found in row {i}")
                continue

            title = title_column.text.strip().split('. ', 1)[-1]  # Remove ranking number prefix
            link = "https://www.imdb.com" + title_column['href'].split('?')[0]

            year_span = row.find("span", class_="sc-b189961a-8")
            year = year_span.text.strip() if year_span else ""

            description = f"Rank #{i+1} trending on IMDb Moviemeter {year}"

            trend = {
                'id': str(uuid.uuid4()),
                'title': title,
                'description': description,
                'link': link,
                'source': 'From IMDb',
                'source_class': 'IMDbTrending',
                'image': "/static/images/default_trendy.svg",
                'video': None,
                'timestamp': datetime.utcnow().isoformat()
            }
            trend['id'] = generate_stable_id(trend)
            results.append(trend)
            logger.debug(f"IMDb: Added trend - {title}")

        if not results:
            logger.warning("IMDb: No trends found, likely due to selector mismatch or JavaScript rendering")
        return results

    except Exception as e:
        logger.error(f"Error fetching IMDb trending: {e}", exc_info=True)
        return []
    
def get_cnn_trending():
    url = "https://www.cnn.com/world"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/91.0.4472.124 Safari/537.36",
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Referer": "https://www.cnn.com/"
    }

    try:
        response = requests.get(url, headers=headers, timeout=10)
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")

        results = []
        # Updated selector based on current CNN structure (as of May 2025)
        articles = soup.select("a.container__link--type-article")
        logger.debug(f"CNN: Found {len(articles)} articles with selector 'a.container__link--type-article'")

        for i, article in enumerate(articles[:10]):
            title = article.get_text(strip=True)
            if not title:
                logger.debug(f"CNN: No title found for article {i}")
                continue

            link = article['href']
            if not link.startswith("http"):
                link = f"https://www.cnn.com{link}"

            trend = {
                "id": str(uuid.uuid4()),
                "title": title,
                "description": f"#{i + 1} on CNN World",
                "link": link,
                "source": "From CNN",
                "source_class": "CNNTrending",
                "image": "/static/images/default_trendy.svg",
                "video": None,
                "timestamp": datetime.utcnow().isoformat()
            }
            trend['id'] = generate_stable_id(trend)
            results.append(trend)
            logger.debug(f"CNN: Added trend - {title}")

        if not results:
            logger.warning("CNN: No trends found, likely due to selector mismatch or empty page")
        return results

    except Exception as e:
        logger.error(f"Error fetching CNN trending: {e}", exc_info=True)
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
        timestamp = datetime.utcnow().isoformat()

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
                "image": "/static/images/default_trendy.svg",
                "video": None,
                "timestamp": timestamp
            }
            trend['id'] = generate_stable_id(trend)  # Assign stable ID inside loop
            results.append(trend)  # Append inside loop

        return results

    except Exception as e:
        print(f"Error fetching Reuters trending: {e}")
        return []
# ---------------------------- AGGREGATE AND CACHE ---------------------------- #
def generate_summary(trend):
    try:
        title = str(trend.get("title") or "Untitled")
        description = str(trend.get("description") or "")
        source = str(trend.get("source") or "an unknown source")
        text = f"{title}. {description}".strip()
        text = re.sub(r'#\w+', '', text)
        text = re.sub(r'\s+', ' ', text).strip(' .|')
        input_length = len(text.split())
        max_length = min(100, max(20, input_length * 2))
        min_length = min(20, max(5, input_length // 2))
        logger.debug(f"Input text: '{text}', input_length={input_length}, max_length={max_length}, min_length={min_length}")
        if input_length < 5:
            logger.debug("Input too short, using custom summary")
            summary_text = f"'{title}' is trending on {source}."
        else:
            result = t5_summarizer(text, max_length=max_length, min_length=min_length, do_sample=False, truncation=True)
            summary_text = result[0]['summary_text'].strip()
        logger.debug(f"Summary output: '{summary_text}'")
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
        source = str(trend.get("source") or "an unknown source")
        title = str(trend.get("title") or "Untitled")
        keywords = [kw for kw in title.lower().split() if len(kw) > 3 and kw not in STOP_WORDS][:3]
        if not keywords:
            keywords = ['trending', source.lower()]
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

@app.route('/')
def home():
    logger.debug("Rendering home page")
    global trends
    random.shuffle(trends)
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
    logger.debug(f"Trends count: {len(trends)}, Sources: {unique_sources}")
    return render_template(
        'index.html',
        trends=trends,
        unique_sources=unique_sources,
        vote_counts=vote_counts_dict
    )

@app.route('/api/trends')
def api_trends():
    logger.debug("Serving /api/trends")
    return jsonify(trends[:2000])

@app.route('/trend/<trend_id>')
def trend_detail(trend_id):
    logger.debug(f"Rendering trend detail for ID: {trend_id}")
    trend = next((t for t in trends if t['id'] == trend_id), None)
    if not trend:
        logger.warning(f"Trend not found: {trend_id}")
        return render_template('404.html'), 404
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
    logger.debug("Processing vote request")
    data = request.json
    trend_id = data.get('trend_id')
    vote_type = data.get('vote_type')
    ip = request.remote_addr
    if not trend_id or not vote_type:
        logger.error(f"Missing trend_id or vote_type: {data}")
        return jsonify({'error': 'Missing trend_id or vote_type'}), 400
    existing_vote = Vote.query.filter_by(trend_id=trend_id, ip_address=ip).first()
    if existing_vote:
        logger.warning(f"Duplicate vote from IP {ip} for trend {trend_id}")
        return jsonify({'error': 'You have already voted for this trend'}), 403
    vote = Vote(trend_id=trend_id, ip_address=ip, vote_type=vote_type)
    db.session.add(vote)
    db.session.commit()
    vote_counts = db.session.query(
        Vote.vote_type,
        db.func.count().label('count')
    ).filter_by(trend_id=trend_id).group_by(Vote.vote_type).all()
    logger.debug(f"Vote recorded for trend {trend_id}: {vote_type}")
    return jsonify({v.vote_type: v.count for v in vote_counts})

def fetch_all_trends():
    global trends
    all_trends = []
    funcs = [get_hacker_news]  # Add other functions as implemented
    for func in funcs:
        try:
            logger.debug(f"Fetching trends from {func.__name__}")
            trends_from_func = func()
            logger.debug(f"Got {len(trends_from_func)} trends from {func.__name__}")
            all_trends.extend(trends_from_func)
        except Exception as e:
            logger.error(f"Error in {func.__name__}: {e}", exc_info=True)
    all_trends.sort(key=lambda x: x['timestamp'], reverse=True)
    trends = all_trends
    logger.debug(f"Total trends fetched: {len(trends)}")

def scheduler():
    while True:
        logger.info(f"[{datetime.utcnow()}] Fetching latest trends...")
        fetch_all_trends()
        time.sleep(30 * 60)

if __name__ == '__main__':
    logger.info("Starting application")
    fetch_all_trends()
    thread = threading.Thread(target=scheduler, daemon=True)
    thread.start()
    port = int(os.environ.get('PORT', 5000))
    app.run(host='0.0.0.0', port=port)