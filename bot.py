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

load_dotenv()

# ─── Config ───────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHANNEL_ID = os.getenv("TELEGRAM_CHANNEL_ID")
MONGO_URI = os.getenv("MONGO_URI")
SCRAPER_API = "https://sky-post-upload-jq4a.onrender.com/latest"
HOMEPAGE_URL = "https://new3.hdhub4u.cl/"
TOP_N = 10

# ─── MongoDB ──────────────────────────────────────────────────────────────────
client = MongoClient(MONGO_URI)
db = client["hdhub4u_bot"]
col = db["sent_posts"]

# ─── Flask ────────────────────────────────────────────────────────────────────
app = Flask(__name__)

@app.route("/")
def health():
    return "HDHub4u Bot is running!", 200


# ─── Helpers ──────────────────────────────────────────────────────────────────

def get_latest_post_urls():
    """Scrape homepage and return top N post URLs."""
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

        print(f"[Scraper] Found {len(urls)} post URLs")
        return urls

    except Exception as e:
        print(f"[Scraper] Homepage scrape failed: {e}")
        return []


def scrape_post(post_url):
    """Call scraper API and return data dict or None."""
    try:
        resp = requests.get(
            SCRAPER_API,
            params={"post": post_url},
            timeout=30
        )
        resp.raise_for_status()
        data = resp.json()

        if not data.get("success"):
            print(f"[API] Scraper returned success=false for {post_url}")
            return None

        return data

    except Exception as e:
        print(f"[API] Failed for {post_url}: {e}")
        return None


SKIP_FROM_LABEL = "4K | SDR | HDR | DV"

def filter_links(raw_links):
    """
    Filter download links:
    - Skip all links with type=watch
    - Once label == SKIP_FROM_LABEL is found, skip that + everything after it
    """
    filtered = []
    for link in raw_links:
        label = link.get("label", "").strip()
        url = link.get("url", "").strip()
        link_type = link.get("type", "download")

        # Skip watch links entirely
        if link_type == "watch":
            continue

        # Skip empty URLs
        if not url:
            continue

        # If this label matches the cutoff, stop processing
        if label == SKIP_FROM_LABEL:
            break

        filtered.append((label, url))

    return filtered


def build_message(data):
    """Build Telegram HTML message from scraped data."""
    title = html.unescape(data.get("title", "Unknown Title"))

    download_links = filter_links(data.get("download_links", []))

    # Build message
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


def send_telegram(message):
    """Send message to Telegram channel."""
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHANNEL_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        resp = requests.post(url, json=payload, timeout=15)
        resp.raise_for_status()
        print(f"[Telegram] Message sent successfully")
        return True

    except Exception as e:
        print(f"[Telegram] Send failed: {e}")
        return False


def is_already_sent(post_url):
    return col.find_one({"url": post_url}) is not None


def mark_as_sent(post_url, title):
    col.update_one(
        {"url": post_url},
        {"$set": {"url": post_url, "title": title}},
        upsert=True
    )


# ─── Main Job ─────────────────────────────────────────────────────────────────

def run_job():
    print("[Job] Starting check...")

    post_urls = get_latest_post_urls()
    if not post_urls:
        print("[Job] No URLs found, skipping.")
        return

    new_count = 0

    # Process in reverse so oldest new post gets sent first
    for url in reversed(post_urls):
        if is_already_sent(url):
            print(f"[Job] Already sent: {url}")
            continue

        print(f"[Job] New post found: {url}")
        data = scrape_post(url)

        if not data:
            print(f"[Job] Scrape failed, skipping: {url}")
            # Still mark to avoid retrying broken posts repeatedly
            mark_as_sent(url, "unknown")
            continue

        message = build_message(data)
        success = send_telegram(message)

        if success:
            mark_as_sent(url, data.get("title", "unknown"))
            new_count += 1
            time.sleep(2)  # small delay between messages

    print(f"[Job] Done. Sent {new_count} new posts.")


# ─── Scheduler ────────────────────────────────────────────────────────────────

def start_scheduler():
    # Run once immediately on startup
    run_job()

    schedule.every(5).minutes.do(run_job)

    while True:
        schedule.run_pending()
        time.sleep(30)


# ─── Entry Point ──────────────────────────────────────────────────────────────

if __name__ == "__main__":
    thread = threading.Thread(target=start_scheduler, daemon=True)
    thread.start()

    port = int(os.getenv("PORT", 8080))
    app.run(host="0.0.0.0", port=port)
