from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
import re
from datetime import datetime, timezone
from typing import Optional, List
from motor.motor_asyncio import AsyncIOMotorClient
import os

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ── MongoDB Setup ──
MONGO_URI = os.getenv("MONGO_URI", "mongodb://localhost:27017")
mongo_client = AsyncIOMotorClient(MONGO_URI)
db = mongo_client["hdhub4u"]
posts_collection = db["posts"]

BASE_URL = "https://new3.hdhub4u.cl/"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

SKIP = ["hdhub4u", "how-to", "whatsapp", "youtube", "imdb", "catimages", "gravatar"]


def err(message: str, status: int = 500, **extra):
    return JSONResponse({"success": False, "error": message, **extra}, status_code=status)


async def fetch_html(url: str) -> str:
    headers = {k: v for k, v in HEADERS.items() if k != "Accept-Encoding"}
    async with httpx.AsyncClient(
        headers=headers,
        follow_redirects=True,
        timeout=30,
        verify=False,
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.content.decode("utf-8", errors="replace")


@app.on_event("startup")
async def startup_event():
    await posts_collection.create_index("post_url", unique=True)


def extract_all_post_urls(home_html: str) -> List[str]:
    anchor_rx = re.compile(r'<a\s+href="(https?://[^"]+)"', re.IGNORECASE)
    seen = set()
    urls = []

    for match in anchor_rx.finditer(home_html):
        url = match.group(1)

        if any(skip in url for skip in SKIP):
            continue
        if "/category/" in url or "/page/" in url:
            continue
        if "hdhub4u.cl" not in url:
            continue

        # Homepage skip
        if re.fullmatch(r"https?://[^/]+/?", url):
            continue

        # Slug kam se kam 5 chars
        if not re.search(r"hdhub4u\.cl/[a-z0-9-]{5,}", url):
            continue

        clean_url = url.split("?")[0].rstrip("/")
        if clean_url not in seen:
            seen.add(clean_url)
            urls.append(clean_url)

    return urls


def extract_title(post_html: str) -> str:
    match = re.search(r"<title>([^<]+)</title>", post_html)
    if match:
        return re.sub(r"\s*[–-]\s*HDHub4u.*", "", match.group(1), flags=re.IGNORECASE).strip()
    return "Unknown"


def extract_thumbnail(post_html: str) -> Optional[str]:
    match = re.search(r'<img[^>]+class="aligncenter"[^>]+src="([^"]+)"', post_html)
    return match.group(1) if match else None


def extract_info(post_html: str) -> dict:
    info = {}
    fields = [
        ("quality",  r"<strong>Quality:\s*</strong>([\s\S]*?)</div>"),
        ("language", r"<strong>Language:\s*</strong>([\s\S]*?)</div>"),
        ("genre",    r"<strong>Genre:\s*</strong>([\s\S]*?)</div>"),
        ("stars",    r"<strong>Stars:\s*</strong>([\s\S]*?)</div>"),
        ("imdb",     r"iMDB Rating:.*?>([\d.]+/\d+)<"),
    ]
    for key, pattern in fields:
        match = re.search(pattern, post_html, re.IGNORECASE)
        if match:
            info[key] = re.sub(r"<[^>]+>", "", match.group(1)).strip()
    return info


def extract_links(post_html: str) -> list:
    links = []

    def extract_from_tag(tag: str):
        block_rx = re.compile(rf"<{tag}[^>]*>([\s\S]*?)</{tag}>", re.IGNORECASE)
        anchor_rx = re.compile(r'<a\s+href="([^"]+)"[^>]*>([\s\S]*?)</a>', re.IGNORECASE)

        for block_match in block_rx.finditer(post_html):
            block_content = block_match.group(1)
            for a_match in anchor_rx.finditer(block_content):
                url = a_match.group(1)
                label = re.sub(r"<[^>]+>", "", a_match.group(2)).strip()
                if not url.startswith("http"):
                    continue
                if any(skip in url for skip in SKIP):
                    continue
                links.append({
                    "label": label,
                    "url": url,
                    "type": "watch" if re.search(r"watch|player", label, re.IGNORECASE) else "download",
                })

    extract_from_tag("h3")
    extract_from_tag("h4")
    return links


async def scrape_and_save_post(post_url: str) -> Optional[dict]:
    try:
        post_html = await fetch_html(post_url)

        doc = {
            "post_url": post_url,
            "title": extract_title(post_html),
            "thumbnail": extract_thumbnail(post_html),
            "info": extract_info(post_html),
            "download_links": extract_links(post_html),
            "scraped_at": datetime.now(timezone.utc).isoformat(),
        }

        await posts_collection.insert_one(doc)
        doc.pop("_id", None)
        return doc

    except Exception as e:
        error_str = str(e).lower()
        if "duplicate" in error_str or "e11000" in error_str:
            return None
        print(f"[ERROR] {post_url}: {e}")
        return None


@app.get("/latest")
async def latest(post: Optional[str] = Query(None)):
    try:
        if post:
            result = await scrape_and_save_post(post)
            if result is None:
                return JSONResponse({
                    "success": True,
                    "message": "Post already exists in DB (duplicate skipped)",
                    "new_posts": [],
                    "new_count": 0,
                })
            return JSONResponse({
                "success": True,
                "new_posts": [result],
                "new_count": 1,
            })

        home_html = await fetch_html(BASE_URL)
        all_urls = extract_all_post_urls(home_html)

        if not all_urls:
            return err("Koi post URL homepage se nahi mila", debug=home_html[:500])

        new_posts = []
        for url in all_urls:
            result = await scrape_and_save_post(url)
            if result is not None:
                new_posts.append(result)

        return JSONResponse({
            "success": True,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "total_found_on_homepage": len(all_urls),
            "new_count": len(new_posts),
            "already_in_db": len(all_urls) - len(new_posts),
            "new_posts": new_posts,
        })

    except httpx.HTTPStatusError as e:
        return err(f"HTTP {e.response.status_code}: {e.request.url}")
    except httpx.TimeoutException:
        return err("Request timed out")
    except Exception as e:
        return err(str(e))


@app.get("/posts")
async def get_all_posts(limit: int = Query(20), skip: int = Query(0)):
    cursor = posts_collection.find({}, {"_id": 0}).sort("scraped_at", -1).skip(skip).limit(limit)
    posts = await cursor.to_list(length=limit)
    total = await posts_collection.count_documents({})
    return JSONResponse({
        "success": True,
        "total_in_db": total,
        "returned": len(posts),
        "posts": posts,
    })


@app.get("/")
async def root():
    return {
        "status": "ok",
        "endpoints": {
            "/latest": "Homepage scrape — naye posts save, sirf naye return",
            "/latest?post=<url>": "Specific post scrape",
            "/posts": "DB ke saare saved posts",
            "/posts?limit=10&skip=0": "Pagination ke saath",
        }
    }
