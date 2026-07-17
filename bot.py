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
import logging

# Setup logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
TELEGRAM_CHANNEL_ID_2 = os.getenv("TELEGRAM_CHANNEL_ID_2")
MONGO_URI = os.getenv("MONGO_URI")

# APIs
SCRAPER_API = "https://sky-post-upload-jq4a.onrender.com/latest"
EXTRA_API = "https://extraapi.tmrbotz.workers.dev/scrape"

# Websites
HOMEPAGE_URL = "https://new3.hdhub4u.cl/"
EXTRAFLIX_URL = "https://e5.extraflix.mobi/"

TOP_N = 10

# Labels for HDHub4u
CUTOFF_LABELS = [
    "4K | SDR | HDR | DV",
    ": Single Episode x264 Links :",
]

SKIP_LABELS = [
    "Drive",
    "Instant",
]

# ─── MongoDB ──────────────────────────────────────────────────────────────────
try:
    client = MongoClient(MONGO_URI, serverSelectionTimeoutMS=5000)
    db = client["hdhub4u_bot"]
    col_hdhub4u = db["sent_posts"]
    col_extraflix = db["sent_posts_extraflix"]
    # Test connection
    client.admin.command('ping')
    logger.info("✅ MongoDB connected successfully")
except Exception as e:
    logger.error(f"❌ MongoDB connection failed: {e}")
    exit(1)

# ─── Flask ────────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def health():
    return {"status": "running", "message": "HDHub4u & ExtraFlix Bot is running!"}, 200

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
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
        resp = requests.get(HOMEPAGE_URL, headers=headers, timeout=30)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        movie_list = soup.select("ul.recent-movies li.thumb")

        urls = []
        for li in movie_list[:TOP_N]:
            a = li.select_one("figcaption a")
            if a and a.get("href"):
                url = a["href"].strip()
                if url:
                    urls.append(url)

        logger.info(f"[HDHub4u] Found {len(urls)} post URLs")
        return urls

    except requests.exceptions.Timeout:
        logger.error("[HDHub4u] Homepage scrape timeout")
        return []
    except requests.exceptions.ConnectionError:
        logger.error("[HDHub4u] Homepage connection error")
        return []
    except Exception as e:
        logger.error(f"[HDHub4u] Homepage scrape failed: {e}")
        return []


def scrape_hdhub4u_post(post_url):
    """Call HDHub4u scraper API and return data dict or None."""
    try:
        logger.info(f"[HDHub4u API] Scraping: {post_url}")
        resp = requests.get(
            SCRAPER_API,
            params={"post": post_url},
            timeout=45
        )
        resp.raise_for_status()
        data = resp.json()

        if not data.get("success"):
            logger.warning(f"[HDHub4u API] success=false for {post_url}")
            return None

        return data

    except requests.exceptions.Timeout:
        logger.error(f"[HDHub4u API] Timeout for {post_url}")
        return None
    except Exception as e:
        logger.error(f"[HDHub4u API] Failed for {post_url}: {e}")
        return None


def filter_hdhub4u_links(raw_links):
    """Filter rules for HDHub4u"""
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
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
        }
        resp = requests.get(EXTRAFLIX_URL, headers=headers, timeout=30)
        resp.raise_for_status()

        soup = BeautifulSoup(resp.text, "html.parser")
        articles = soup.select("article.entry-card")

        urls = []
        for article in articles[:TOP_N]:
            title_link = article.select_one("h2.entry-title a")
            if title_link and title_link.get("href"):
                url = title_link["href"].strip()
                if url:
                    urls.append(url)

        logger.info(f"[ExtraFlix] Found {len(urls)} post URLs")
        return urls

    except requests.exceptions.Timeout:
        logger.error("[ExtraFlix] Homepage scrape timeout")
        return []
    except requests.exceptions.ConnectionError:
        logger.error("[ExtraFlix] Homepage connection error")
        return []
    except Exception as e:
        logger.error(f"[ExtraFlix] Homepage scrape failed: {e}")
        return []


def scrape_extraflix_post(post_url):
    """Call ExtraFlix API and return data dict or None."""
    try:
        full_url = f"{EXTRA_API}?url={post_url}"
        logger.info(f"[ExtraFlix API] Scraping: {post_url}")
        resp = requests.get(full_url, timeout=45)
        resp.raise_for_status()
        data = resp.json()

        if not data.get("success"):
            logger.warning(f"[ExtraFlix API] success=false for {post_url}")
            return None

        return data

    except requests.exceptions.Timeout:
        logger.error(f"[ExtraFlix API] Timeout for {post_url}")
        return None
    except Exception as e:
        logger.error(f"[ExtraFlix API] Failed for {post_url}: {e}")
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
            
            if mirrors:
                first_mirror = mirrors[0]
                lines.append(f'• <a href="{first_mirror}">{html.escape(file_info)}</a>')
                if file_size:
                    lines.append(f'  <i>({html.escape(file_size)})</i>')
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
        logger.info(f"[Telegram] Message sent to {chat_id}")
        return True

    except Exception as e:
        logger.error(f"[Telegram] Send failed to {chat_id}: {e}")
        return False


def is_already_sent(collection, post_url):
    return collection.find_one({"url": post_url}) is not None


def mark_as_sent(collection, post_url, title):
    collection.update_one(
        {"url": post_url},
        {"$set": {"url": post_url, "title": title, "sent_at": time.time()}},
        upsert=True
    )


# ─── Main Jobs ──────────────────────────────────────────────────────────────

def run_hdhub4u_job():
    logger.info("🔄 Starting HDHub4u check...")

    post_urls = get_latest_hdhub4u_urls()
    if not post_urls:
        logger.warning("[HDHub4u] No URLs found")
        return

    new_count = 0
    for url in reversed(post_urls):
        try:
            if is_already_sent(col_hdhub4u, url):
                logger.debug(f"[HDHub4u] Already sent: {url}")
                continue

            logger.info(f"[HDHub4u] New post: {url}")
            data = scrape_hdhub4u_post(url)

            if not data:
                logger.warning(f"[HDHub4u] Scrape failed, marking: {url}")
                mark_as_sent(col_hdhub4u, url, "unknown")
                continue

            message = build_hdhub4u_message(data)
            success = send_telegram(message, TELEGRAM_CHANNEL_ID)

            if success:
                mark_as_sent(col_hdhub4u, url, data.get("title", "unknown"))
                new_count += 1
                time.sleep(2)
        except Exception as e:
            logger.error(f"[HDHub4u] Error processing {url}: {e}")

    logger.info(f"[HDHub4u] Sent {new_count} new posts")


def run_extraflix_job():
    logger.info("🔄 Starting ExtraFlix check...")

    post_urls = get_latest_extraflix_urls()
    if not post_urls:
        logger.warning("[ExtraFlix] No URLs found")
        return

    new_count = 0
    for url in reversed(post_urls):
        try:
            if is_already_sent(col_extraflix, url):
                logger.debug(f"[ExtraFlix] Already sent: {url}")
                continue

            logger.info(f"[ExtraFlix] New post: {url}")
            data = scrape_extraflix_post(url)

            if not data:
                logger.warning(f"[ExtraFlix] Scrape failed, marking: {url}")
                mark_as_sent(col_extraflix, url, "unknown")
                continue

            message = build_extraflix_message(data)
            success = send_telegram(message, TELEGRAM_CHANNEL_ID_2)

            if success:
                mark_as_sent(col_extraflix, url, data.get("title", "unknown"))
                new_count += 1
                time.sleep(2)
        except Exception as e:
            logger.error(f"[ExtraFlix] Error processing {url}: {e}")

    logger.info(f"[ExtraFlix] Sent {new_count} new posts")


def run_all_jobs():
    """Run both jobs together"""
    logger.info("="*50)
    logger.info("🚀 Running both jobs...")
    logger.info("="*50)
    
    run_hdhub4u_job()
    logger.info("-"*30)
    run_extraflix_job()
    
    logger.info("="*50)
    logger.info("✅ Both jobs completed!")
    logger.info("="*50)


# ─── Scheduler ────────────────────────────────────────────────────────────────

def start_scheduler():
    # Wait 5 seconds before first run to let Flask start
    time.sleep(5)
    logger.info("🚀 Running initial job...")
    run_all_jobs()

    # Schedule every 10 minutes
    schedule.every(10).minutes.do(run_all_jobs)
    logger.info("⏰ Scheduler started - running every 10 minutes")

    while True:
        try:
            schedule.run_pending()
            time.sleep(30)
        except Exception as e:
            logger.error(f"Scheduler error: {e}")
            time.sleep(60)


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    # Check environment variables
    required_vars = ["TELEGRAM_BOT_TOKEN", "TELEGRAM_CHANNEL_ID", "TELEGRAM_CHANNEL_ID_2", "MONGO_URI"]
    missing_vars = [var for var in required_vars if not os.getenv(var)]
    
    if missing_vars:
        logger.error(f"❌ Missing: {', '.join(missing_vars)}")
        exit(1)
    
    logger.info("✅ All environment variables set")
    
    # Start scheduler
    scheduler_thread = threading.Thread(target=start_scheduler, daemon=True)
    scheduler_thread.start()
    logger.info("🔄 Scheduler thread started")

    # Start Flask
    port = int(os.getenv("PORT", 8080))
    logger.info(f"🌐 Starting Flask server on port {port}")
    app.run(host="0.0.0.0", port=port, debug=False, use_reloader=False)
