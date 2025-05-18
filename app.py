from flask import Flask, render_template, jsonify
import requests
from bs4 import BeautifulSoup
import os
import random
import threading
import time
from datetime import datetime
import uuid
from urllib.parse import urljoin, quote_plus
from datetime import datetime, timezone
from flask import request, jsonify
import json
from flask_sqlalchemy import SQLAlchemy

app = Flask(__name__)

trends = []
YOUTUBE_API_KEY = os.getenv('YOUTUBE_API_KEY')

db = SQLAlchemy()

class Trend(db.Model):
    id = db.Column(db.String, primary_key=True)  # Use the same ID as in your trend data
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
    vote_type = db.Column(db.String, nullable=False)  # 'like' or 'dislike'
    timestamp = db.Column(db.DateTime, default=datetime.utcnow)

    __table_args__ = (
        db.UniqueConstraint('trend_id', 'ip_address', name='unique_vote_per_ip'),
    )

app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///trendy.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False
db.init_app(app)

with app.app_context():
    db.create_all()

VOTES_FILE = 'votes.json'

def load_votes():
    try:
        with open(VOTES_FILE, 'r') as f:
            return json.load(f)
    except FileNotFoundError:
        return {}

def save_votes(votes):
    with open(VOTES_FILE, 'w') as f:
        json.dump(votes, f, indent=2)

@app.route('/api/vote', methods=['POST'])
def vote():
    data = request.json
    trend_id = data.get('trend_id')
    vote_type = data.get('vote_type')
    ip = request.remote_addr

    if vote_type not in ['like', 'dislike']:
        return jsonify({'error': 'Invalid vote type'}), 400

    existing_vote = Vote.query.filter_by(trend_id=trend_id, ip_address=ip).first()
    if existing_vote:
        return jsonify({'error': 'You have already voted for this trend'}), 403

    vote = Vote(trend_id=trend_id, ip_address=ip, vote_type=vote_type)
    db.session.add(vote)
    db.session.commit()

    like_count = Vote.query.filter_by(trend_id=trend_id, vote_type='like').count()
    dislike_count = Vote.query.filter_by(trend_id=trend_id, vote_type='dislike').count()

    return jsonify({'like': like_count, 'dislike': dislike_count})


def time_ago(timestamp_str):
    # Parse the ISO timestamp string back to datetime object
    past = datetime.fromisoformat(timestamp_str).replace(tzinfo=timezone.utc)
    now = datetime.utcnow().replace(tzinfo=timezone.utc)
    diff = now - past

    seconds = diff.total_seconds()
    minutes = seconds // 60
    hours = seconds // 3600
    days = seconds // 86400

    if seconds < 60:
        return f"{int(seconds)} seconds ago"
    elif minutes < 60:
        return f"{int(minutes)} minutes ago"
    elif hours < 24:
        return f"{int(hours)} hours ago"
    else:
        return f"{int(days)} days ago"
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

        # Hacker News does not provide description easily on front page, so blank
        description = ''

        results.append({
            'id': str(uuid.uuid4()),
            'title': title,
            'description': description,
            'link': link,
            'source': 'From Hacker News',
            'source_class': 'HackerTrending',
            'image': '/static/images/default_trendy.svg',
            'video': None,
            'timestamp': datetime.utcnow().isoformat()
        })
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

        results.append({
            'id': str(uuid.uuid4()),
            'title': title,
            'description': description,
            'link': link,
            'source': 'From GitHub',
            'source_class': 'GithubTrending',
            'image': '/static/images/default_trendy.svg',
            'video': None,
            'timestamp': datetime.utcnow().isoformat()
        })
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

        results.append({
            'id': str(uuid.uuid4()),
            'title': title,
            'description': description,
            'link': reddit_url,
            'source': 'From Reddit',
            'source_class': 'RedditTrending',
            'image': image_url if image_url else '/static/images/default_trendy.svg',
            'video': video_url,
            'timestamp': datetime.utcnow().isoformat()
        })
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

        results.append({
            'id': str(uuid.uuid4()),
            'title': title,
            'description': description,
            'link': link,
            'source': 'From TechCrunch',
            'source_class': 'TechcrunchTrending',
            'image': image,
            'video': None,
            'timestamp': datetime.utcnow().isoformat()
        })

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

        results.append({
            'id': str(uuid.uuid4()),
            'title': title,
            'description': description,
            'link': link,
            'source': 'From Stack Overflow',
            'source_class': 'StackoverflowTrending',
            'image': '/static/images/default_trendy.svg',
            'video': None,
            'timestamp': datetime.utcnow().isoformat()
        })
    return results

def get_devto_latest():
    url = 'https://dev.to/'
    soup = BeautifulSoup(requests.get(url).text, 'html.parser')
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
            results.append({
                'id': str(uuid.uuid4()),
                'title': title,
                'description': description,
                'link': link,
                'source': 'From Dev.to',
                'source_class': 'DevtoTrending',
                'image': '/static/images/default_trendy.svg',
                'video': None,
                'timestamp': datetime.utcnow().isoformat()
            })
    return results

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
        results.append({
            'id': str(uuid.uuid4()),
            'title': title,
            'description': description,
            'link': link,
            'source': 'From Medium Technology',
            'source_class': 'MediumtechTrending',
            'image': '/static/images/default_trendy.svg',
            'video': None,
            'timestamp': datetime.utcnow().isoformat()
        })
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
        results.append({
            'id': str(uuid.uuid4()),
            'title': title,
            'description': description,
            'link': link,
            'source': 'From Lobsters',
            'source_class': 'LobstersTrending',
            'image': '/static/images/default_trendy.svg',
            'video': None,
            'timestamp': datetime.utcnow().isoformat()
        })
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
        results.append({
            'id': str(uuid.uuid4()),
            'title': title,
            'description': description,
            'link': link,
            'source': 'From Slashdot',
            'source_class': 'SlashdotTrending',
            'image': '/static/images/default_trendy.svg',
            'video': None,
            'timestamp': datetime.utcnow().isoformat()
        })
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
        results.append({
            'id': str(uuid.uuid4()),
            'title': title,
            'description': description,
            'link': link,
            'source': 'From Digg',
            'source_class': 'DiggTrending',
            'image': image if image else '/static/images/default_trendy.svg',
            'video': None,
            'timestamp': datetime.utcnow().isoformat()
        })
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
        results.append({
            'id': str(uuid.uuid4()),
            'title': title,
            'description': description,
            'link': link,
            'source': 'From BBC',
            'source_class': 'BBCTrending',
            'image': '/static/images/default_trendy.svg',
            'video': None,
            'timestamp': datetime.utcnow().isoformat()
        })
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
        results.append({
            'id': str(uuid.uuid4()),
            'title': text[:50] + ('...' if len(text) > 50 else ''),
            'description': text,
            'link': 'https://twitter.com/search?q=%23technology',
            'source': 'From X',
            'source_class': 'XTrending',
            'image': '/static/images/default_trendy.svg',
            'video': None,
            'timestamp': datetime.utcnow().isoformat()
        })
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
        
        results.append({
            'id': str(uuid.uuid4()),
            'title': title,
            'description': description,
            'link': link,
            'source': 'From YouTube',
            'source_class': 'YouTubeTrending',
            'image': image,
            'video': None,
            'timestamp': datetime.utcnow().isoformat()
        })
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

        results.append({
            'id': str(uuid.uuid4()),
            'title': title,
            'description': description,
            'link': link,
            'source': 'From Ars Technica',
            'source_class': 'ArsTechnicaTrending',
            'image': '/static/images/default_trendy.svg',
            'video': None,
            'timestamp': datetime.utcnow().isoformat()
        })
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

        results.append({
            'id': str(uuid.uuid4()),
            'title': title,
            'description': description,
            'link': link,
            'source': 'From Wired',
            'source_class': 'WiredTrending',
            'image': '/static/images/default_trendy.svg',
            'video': None,
            'timestamp': datetime.utcnow().isoformat()
        })
    return results

def fetch_goodreads_trending():
    url = "https://www.goodreads.com/book/popular_by_date/2025"
    headers = {
        "User-Agent": "Mozilla/5.0"
    }
    response = requests.get(url, headers=headers)
    soup = BeautifulSoup(response.text, "html.parser")

    trends = []

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

        trends.append(trend)

    return trends

def fetch_steam_charts():
    url = "https://steamcharts.com/top"
    headers = {
        "User-Agent": "Mozilla/5.0"
    }
    response = requests.get(url, headers=headers)
    soup = BeautifulSoup(response.text, "html.parser")

    trends = []
    timestamp = datetime.utcnow().isoformat()

    rows = soup.select("table.common-table tbody tr")[:20]  # Top 20 games

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

        trends.append(trend)

    return trends

def scrape_spotify_charts():
    url = 'https://spotifycharts.com/regional/global/daily/latest'
    response = requests.get(url)
    soup = BeautifulSoup(response.text, 'html.parser')

    rows = soup.select('table.chart-table tbody tr')

    trends = []

    for row in rows:
        title = row.select_one('.chart-table-track strong').text.strip()
        artist = row.select_one('.chart-table-track span').text.strip().replace('by ', '')
        description = f"{title} by {artist}"
        link_tag = row.select_one('.chart-table-image a')
        link = link_tag['href'] if link_tag else url
        image_style = row.select_one('.chart-table-image')['style']
        image_url = image_style.split("url('")[1].split("')")[0] if "url('" in image_style else ""

        trend = {
            'id': f"spotify-{title.lower().replace(' ', '-')[:50]}",
            'title': title,
            'image': image_url,
            'description': description,
            'link': link,
            'source': 'Spotify Charts',
            "source_class": "SpotifyTrending",
            'timestamp': datetime.utcnow().isoformat()
        }
        trends.append(trend)

    return trends
# ---------------------------- AGGREGATE AND CACHE ---------------------------- #

def fetch_all_trends():
    global trends
    all_trends = []
    funcs = [
        get_hacker_news,
        get_github_trending,
        get_reddit_top,
        get_techcrunch,
        get_stackoverflow_trending,
        get_devto_latest,
        get_medium_technology,
        get_lobsters,
        get_slashdot,
        get_digg_popular,
        get_bbc_trending,
        get_twitter_trending,
        lambda: get_youtube_trending(YOUTUBE_API_KEY),
        get_ars_technica,
        get_wired,
        fetch_goodreads_trending,
        fetch_steam_charts,
        scrape_spotify_charts
    ]
    for func in funcs:
        try:
            all_trends.extend(func())
        except Exception as e:
            print(f"Error in {func.__name__}: {e}")

    # Sort by timestamp descending (newest first)
    all_trends.sort(key=lambda x: x['timestamp'], reverse=True)
    trends = all_trends

def scheduler():
    while True:
        print(f"[{datetime.utcnow()}] Fetching latest trends...")
        fetch_all_trends()
        time.sleep(30 * 60)  # every 30 minutes

# ---------------------------- FLASK ROUTES ---------------------------- #

@app.route('/')
def home():
    trends_hn = get_hacker_news()
    trends_gh = get_github_trending()
    trends_tc = get_techcrunch()
    trends_so = get_stackoverflow_trending()
    trends_dt = get_devto_latest()
    trends_mt = get_medium_technology()
    trends_ls = get_lobsters()
    trends_sd = get_slashdot()
    trends_dp = get_digg_popular()
    trends_bbc = get_bbc_trending()
    trends_twitter = get_twitter_trending()
    trends_youtube = get_youtube_trending(YOUTUBE_API_KEY)
    trends_reddit = get_reddit_top()
    trends_at = get_ars_technica()
    trends_wired = get_wired()
    trends_gr = fetch_goodreads_trending()
    trends_sc = fetch_steam_charts()
    trends_sy = scrape_spotify_charts()

    all_trends = trends_hn + trends_gh + trends_youtube + trends_reddit + trends_tc + trends_so + trends_dt + trends_mt + trends_ls + trends_sd + trends_dp + trends_bbc + trends_twitter + trends_at + trends_wired + trends_gr + trends_sc + trends_sy

    random.shuffle(all_trends)
    unique_sources = sorted(set(trend['source'] for trend in all_trends))
    
    votes = db.session.query(
        Vote.trend_id,
        db.func.count(db.case((Vote.vote_type == 'like', 1))).label('like'),
        db.func.count(db.case((Vote.vote_type == 'dislike', 1))).label('dislike')
    ).group_by(Vote.trend_id).all()

    vote_counts = {v.trend_id: {'like': v.like, 'dislike': v.dislike} for v in votes}

    return render_template('index.html', trends=all_trends, unique_sources=unique_sources, vote_counts=vote_counts)


@app.route('/api/trends')
def api_trends():
    return jsonify(trends[:2000])

# ---------------------------- MAIN ---------------------------- #

if __name__ == '__main__':
    fetch_all_trends()  # initial fetch
    thread = threading.Thread(target=scheduler, daemon=True)
    thread.start()
    # app.run(debug=True, port=5000)
    app.run()
    
