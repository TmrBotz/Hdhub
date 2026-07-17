import os
import requests
import schedule
import time
import threading
import html
from bs4 import BeautifulSoup
from pymongo import MongoClient
from flask import Flask
from dotenv import load_dotenv
import json
from urllib.parse import urlparse, urljoin

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")  # Channel 1 (HDHub4u style)
TELEGRAM_CHANNEL_ID_2 = os.getenv("TELEGRAM_CHANNEL_ID_2")  # Channel 2 (ExtraFlix)
MONGO_URI = os.getenv("MONGO_URI")

# APIs
SCRAPER_API = "https://sky-post-upload-jq4a.onrender.com/latest"
EXTRA_API = "https://extraapi.tmrbotz.workers.dev/scrape"

# Websites
HOMEPAGE_URL = "https://new3.hdhub4u.cl/"
EXTRAFLIX_URL = "https://e5.extraflix.mobi/"

TOP_N = 10

# Labels jahan se aage sab skip ho jata hai (for HDHub4u)
CUTOFF_LABELS = [
    "4K | SDR | HDR | DV",
    ": Single Episode x264 Links :",
]

# Ye labels hamesha skip honge chahe kahan bhi aayein (for HDHub4u)
SKIP_LABELS = [
    "Drive",
    "Instant",
]

# ─── MongoDB ──────────────────────────────────────────────────────────────────
client = MongoClient(MONGO_URI)
db = client["hdhub4u_bot"]
col_hdhub4u = db["sent_posts"]
col_extraflix = db["sent_posts_extraflix"]

# ─── Flask ────────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def health():
    return "HDHub4u & ExtraFlix Bot is running!", 200

@app.route("/status")
def status():
    return {
        "status": "running",
        "hdhub4u_count": col_hdhub4u.count_documents({}),
        "extraflix_count": col_extraflix.count_documents({})
    }, 200

# ─── HDHub4u Functions ──────────────────────────────────────────────────────

def get_latest_hdhub4u_urls():
    """Scrape HDHub4u homepage and return top N post URLs."""
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        }
        resp = requests.get(HOMEPAGE_URL, headers=headers, timeout=20)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        movie_list = soup.select("ul.recent-movies li.thumb")

        urls = []
        for li in movie_list[:TOP_N]:
            a = li.select_one("figcaption a")
            if a and a.get("href"):
                urls.append(a["href"].strip())

        print(f"[HDHub4u] Found {len(urls)} post URLs")
        return urls

    except Exception as e:
        print(f"[HDHub4u] Homepage scrape failed: {e}")
        return []


def scrape_hdhub4u_post(post_url):
    """Call HDHub4u scraper API and return data dict or None."""
    try:
        resp = requests.get(
            SCRAPER_API,
            params={"post": post_url},
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json()

        if not data.get("success"):
            print(f"[HDHub4u API] Scraper returned success=false for {post_url}")
            return None

        return data

    except Exception as e:
        print(f"[HDHub4u API] Failed for {post_url}: {e}")
        return None


def filter_hdhub4u_links(raw_links):
    """
    Filter rules for HDHub4u:
    1. type=watch → hamesha skip
    2. label in SKIP_LABELS → hamesha skip (Drive, Instant)
    3. label in CUTOFF_LABELS → yahan se aage sab skip (break)
    4. empty url → skip
    """
    filtered = []
    for link in raw_links:
        label = link.get("label", "").strip()
        url = link.get("url", "").strip()
        ltype = link.get("type", "download")

        if ltype == "watch":
            continue

        if not url:
            continue

        if label in CUTOFF_LABELS:
            break

        if label in SKIP_LABELS:
            continue

        filtered.append((label, url))

    return filtered


def build_hdhub4u_message(data):
    """Build Telegram HTML message from HDHub4u scraped data."""
    title = html.unescape(data.get("title", "Unknown Title"))
    download_links = filter_hdhub4u_links(data.get("download_links", []))

    lines = []
    lines.append(f"<b>{html.escape(title)}</b>")
    lines.append("")

    if download_links:
        lines.append("📥 <b>Download Links:</b>")
        for label, url in download_links:
            lines.append(f'• <a href="{url}">{html.escape(label)}</a>')
    else:
        lines.append("⚠️ No download links found.")

    return "\n".join(lines)


# ─── ExtraFlix Functions ────────────────────────────────────────────────────

def get_latest_extraflix_urls():
    """Scrape ExtraFlix homepage and return top N post URLs."""
    try:
        headers = {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/124.0.0.0 Safari/537.36"
            )
        }
        resp = requests.get(EXTRAFLIX_URL, headers=headers, timeout=20)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        # ExtraFlix uses article entries with class "entry-card"
        articles = soup.select("article.entry-card")

        urls = []
        for article in articles[:TOP_N]:
            title_link = article.select_one("h2.entry-title a")
            if title_link and title_link.get("href"):
                urls.append(title_link["href"].strip())

        print(f"[ExtraFlix] Found {len(urls)} post URLs")
        return urls

    except Exception as e:
        print(f"[ExtraFlix] Homepage scrape failed: {e}")
        return []


def scrape_extraflix_post(post_url):
    """Call ExtraFlix API and return data dict or None."""
    try:
        # API expects URL as query parameter
        full_url = f"{EXTRA_API}?url={post_url}"
        resp = requests.get(full_url, timeout=30)
        resp.raise_for_status()
        data = resp.json()

        if not data.get("success"):
            print(f"[ExtraFlix API] Scraper returned success=false for {post_url}")
            return None

        return data

    except Exception as e:
        print(f"[ExtraFlix API] Failed for {post_url}: {e}")
        return None


def build_extraflix_message(data):
    """Build Telegram HTML message from ExtraFlix scraped data."""
    title = html.unescape(data.get("title", "Unknown Title"))
    links = data.get("links", [])
    
    lines = []
    lines.append(f"<b>{html.escape(title)}</b>")
    lines.append("")
    
    if links:
        lines.append("📥 <b>Download Links:</b>")
        for link in links:
            file_info = link.get("fileInfo", "Unknown")
            file_size = link.get("fileSize", "")
            mirrors = link.get("mirrors", [])
            
            # Show first mirror only (or all if you want)
            if mirrors:
                first_mirror = mirrors[0]
                lines.append(f'• <a href="{first_mirror}">{html.escape(file_info)}</a>')
                if file_size:
                    lines.append(f'  <i>({html.escape(file_size)})</i>')
                # If there are multiple mirrors, show count
                if len(mirrors) > 1:
                    lines.append(f'  <i>+ {len(mirrors)-1} more mirror(s)</i>')
                lines.append("")
    else:
        lines.append("⚠️ No download links found.")
    
    return "\n".join(lines)


# ─── Telegram Functions ─────────────────────────────────────────────────────

def send_telegram(message, chat_id):
    """Send message to Telegram channel."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": chat_id,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        print(f"[Telegram] Message sent successfully to {chat_id}")
        return True

    except Exception as e:
        print(f"[Telegram] Send failed to {chat_id}: {e}")
        return False


def is_already_sent(collection, post_url):
    return collection.find_one({"url": post_url}) is not None


def mark_as_sent(collection, post_url, title):
    collection.update_one(
        {"url": post_url},
        {"$set": {"url": post_url, "title": title, "sent_at": time.time()}},
        upsert=True
    )


# ─── Main Job for HDHub4u ──────────────────────────────────────────────────

def run_hdhub4u_job():
    print("[Job] Starting HDHub4u check...")

    post_urls = get_latest_hdhub4u_urls()
    if not post_urls:
        print("[Job HDHub4u] No URLs found, skipping.")
        return

    new_count = 0

    for url in reversed(post_urls):
        if is_already_sent(col_hdhub4u, url):
            print(f"[Job HDHub4u] Already sent: {url}")
            continue

        print(f"[Job HDHub4u] New post found: {url}")
        data = scrape_hdhub4u_post(url)

        if not data:
            print(f"[Job HDHub4u] Scrape failed, marking & skipping: {url}")
            mark_as_sent(col_hdhub4u, url, "unknown")
            continue

        message = build_hdhub4u_message(data)
        success = send_telegram(message, TELEGRAM_CHANNEL_ID)

        if success:
            mark_as_sent(col_hdhub4u, url, data.get("title", "unknown"))
            new_count += 1
            time.sleep(2)

    print(f"[Job HDHub4u] Done. Sent {new_count} new posts.")


# ─── Main Job for ExtraFlix ─────────────────────────────────────────────────

def run_extraflix_job():
    print("[Job] Starting ExtraFlix check...")

    post_urls = get_latest_extraflix_urls()
    if not post_urls:
        print("[Job ExtraFlix] No URLs found, skipping.")
        return

    new_count = 0

    for url in reversed(post_urls):
        if is_already_sent(col_extraflix, url):
            print(f"[Job ExtraFlix] Already sent: {url}")
            continue

        print(f"[Job ExtraFlix] New post found: {url}")
        data = scrape_extraflix_post(url)

        if not data:
            print(f"[Job ExtraFlix] Scrape failed, marking & skipping: {url}")
            mark_as_sent(col_extraflix, url, "unknown")
            continue

        message = build_extraflix_message(data)
        success = send_telegram(message, TELEGRAM_CHANNEL_ID_2)

        if success:
            mark_as_sent(col_extraflix, url, data.get("title", "unknown"))
            new_count += 1
            time.sleep(2)

    print(f"[Job ExtraFlix] Done. Sent {new_count} new posts.")


# ─── Combined Job ────────────────────────────────────────────────────────────

def run_all_jobs():
    """Run both jobs together"""
    print("\n" + "="*50)
    print("Running both jobs...")
    print("="*50 + "\n")
    
    run_hdhub4u_job()
    print("\n" + "-"*30 + "\n")
    run_extraflix_job()
    
    print("\n" + "="*50)
    print("Both jobs completed!")
    print("="*50 + "\n")


# ─── Scheduler ────────────────────────────────────────────────────────────────

def start_scheduler():
    # Startup pe turant ek baar run karo
    run_all_jobs()

    # Schedule both jobs every 10 minutes
    schedule.every(10).minutes.do(run_all_jobs)

    while True:
        schedule.run_pending()
        time.sleep(30)


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Check required environment variables
    required_vars = [
        "TELEGRAM_BOT_TOKEN",
        "TELEGRAM_CHANNEL_ID",
        "TELEGRAM_CHANNEL_ID_2",
        "MONGO_URI"
    ]
    
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    if missing_vars:
        print(f"❌ Missing required environment variables: {', '.join(missing_vars)}")
        print("Please set them in your .env file or environment.")
        exit(1)
    
    print("✅ All environment variables set. Starting bot...")
    
    # Start scheduler in background thread
    thread = threading.Thread(target=start_scheduler, daemon=True)
    thread.start()

    # Start Flask app
    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
