from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
import re
from datetime import datetime, timezone
from typing import Optional
from pymongo import MongoClient
import os
from dotenv import load_dotenv

# Load environment variables
load_dotenv()

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

# ============================================================
# CONFIG
# ============================================================
BASE_URL = "https://new3.hdhub4u.cl/"

MONGODB_URI = os.getenv("MONGODB_URI")
DB_NAME = "hdhub4u"
COLLECTION_NAME = "posts"

# MongoDB client
mongo_client = MongoClient(MONGODB_URI)
db = mongo_client[DB_NAME]
collection = db[COLLECTION_NAME]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "en-US,en;q=0.9",
    "Accept-Encoding": "gzip, deflate, br",
    "Connection": "keep-alive",
    "Upgrade-Insecure-Requests": "1",
    "Sec-Fetch-Dest": "document",
    "Sec-Fetch-Mode": "navigate",
    "Sec-Fetch-Site": "none",
    "Sec-Fetch-User": "?1",
    "Cache-Control": "max-age=0",
}

SKIP = ["hdhub4u", "how-to", "whatsapp", "youtube", "imdb", "catimages", "gravatar"]


# ============================================================
# HELPER FUNCTIONS
# ============================================================

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


def extract_post_id(post_url: str) -> Optional[str]:
    """Extract post ID from URL (slug after domain)"""
    # URL: https://new3.hdhub4u.cl/khushkhabri-2026-punjabi-webrip-full-movie/
    # Returns: khushkhabri-2026-punjabi-webrip-full-movie
    match = re.search(r'\.cl/([^/?]+)/?$', post_url)
    return match.group(1) if match else None


def extract_all_post_urls(home_html: str) -> list:
    """Extract all post URLs from homepage"""
    urls = []
    
    patterns = [
        r'class="recent-movies"[\s\S]*?<a\s+href="(https?://[^"]+)"',
        r'class="thumb[^"]*"[\s\S]*?<a\s+href="(https?://new[0-9]*\.hdhub4u\.cl/[^"]+)"',
        r'<figure>[\s\S]*?<a\s+href="(https?://new[0-9]*\.hdhub4u\.cl/[^"]+)"',
        r'href="(https?://new[0-9]*\.hdhub4u\.cl/[a-z0-9][a-z0-9-]{5,}/?)"',
    ]
    
    for pattern in patterns:
        matches = re.findall(pattern, home_html)
        for url in matches:
            # Filter unwanted URLs
            if ("/category/" not in url and 
                "/page/" not in url and 
                not url.endswith(".cl/") and
                not url.endswith(".cl") and
                not any(skip in url for skip in SKIP)):
                urls.append(url)
    
    # Remove duplicates while preserving order
    return list(dict.fromkeys(urls))


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


# ============================================================
# MAIN ENDPOINT: GET /latest
# ============================================================

@app.get("/latest")
async def latest(post: Optional[str] = Query(None)):
    """
    Fetch latest post(s) from HDHub4u
    
    - If no 'post' parameter: Scrape homepage, find all new posts, process only the latest one
    - If 'post' parameter provided: Process that specific URL (with duplicate check)
    """
    try:
        # ── CASE 1: Specific URL provided ──
        if post:
            post_url = post
            post_id = extract_post_id(post_url)
            
            if not post_id:
                return err("Invalid post URL - could not extract post ID")
            
            # Check if already exists in MongoDB
            existing = collection.find_one({"post_id": post_id})
            if existing:
                return JSONResponse({
                    "success": True,
                    "message": "Post already exists in database",
                    "post_id": post_id,
                    "post_url": post_url,
                    "already_saved": True,
                    "saved_at": existing.get("scraped_at")
                })
            
            # Scrape specific post
            post_html = await fetch_html(post_url)
            
            post_data = {
                "post_id": post_id,
                "post_url": post_url,
                "title": extract_title(post_html),
                "thumbnail": extract_thumbnail(post_html),
                "info": extract_info(post_html),
                "download_links": extract_links(post_html),
                "scraped_at": datetime.now(timezone.utc).isoformat()
            }
            
            # Save to MongoDB
            collection.insert_one(post_data)
            
            return JSONResponse({
                "success": True,
                "message": "Post saved successfully",
                "post": post_data
            })
        
        # ── CASE 2: No URL - Get latest from homepage ──
        else:
            # Step 1: Fetch homepage
            home_html = await fetch_html(BASE_URL)
            all_post_urls = extract_all_post_urls(home_html)
            
            if not all_post_urls:
                return err("No posts found on homepage")
            
            # Step 2: Get existing post IDs from MongoDB
            existing_docs = collection.find({}, {"post_id": 1, "_id": 0}).to_list()
            existing_ids = set(doc["post_id"] for doc in existing_docs)
            
            # Step 3: Filter new posts
            new_posts = []
            for url in all_post_urls:
                post_id = extract_post_id(url)
                if post_id and post_id not in existing_ids:
                    new_posts.append({
                        "post_url": url,
                        "post_id": post_id
                    })
            
            # Step 4: If no new posts
            if not new_posts:
                return JSONResponse({
                    "success": True,
                    "message": "No new posts found - all already in database",
                    "total_posts_found": len(all_post_urls),
                    "new_posts_count": 0,
                    "posts": []
                })
            
            # Step 5: Process ONLY FIRST new post (latest)
            first_post = new_posts[0]
            
            # Scrape post page
            post_html = await fetch_html(first_post["post_url"])
            
            # Extract all data
            post_data = {
                "post_id": first_post["post_id"],
                "post_url": first_post["post_url"],
                "title": extract_title(post_html),
                "thumbnail": extract_thumbnail(post_html),
                "info": extract_info(post_html),
                "download_links": extract_links(post_html),
                "scraped_at": datetime.now(timezone.utc).isoformat()
            }
            
            # Save to MongoDB
            collection.insert_one(post_data)
            
            # Return response
            return JSONResponse({
                "success": True,
                "message": "New post saved successfully",
                "total_posts_found": len(all_post_urls),
                "new_posts_available": len(new_posts),
                "remaining_new_posts": len(new_posts) - 1,
                "post": post_data,
                "new_posts_list": [
                    {"post_id": p["post_id"], "post_url": p["post_url"]} 
                    for p in new_posts[1:6]  # Show next 5 new posts
                ]
            })
    
    except httpx.HTTPStatusError as e:
        return err(f"HTTP {e.response.status_code}: {e.request.url}")
    except httpx.TimeoutException:
        return err("Request timed out")
    except Exception as e:
        return err(str(e))


# ============================================================
# OTHER ENDPOINTS
# ============================================================

@app.get("/")
async def root():
    return {
        "status": "ok", 
        "service": "HDHub4u Scraper",
        "endpoints": [
            "/latest - Get latest post (auto-detect)",
            "/latest?post=<url> - Get specific post",
            "/all - Get all posts from homepage (without saving)",
            "/saved - Get saved posts from database"
        ]
    }


@app.get("/all")
async def get_all_posts():
    """Get all posts from homepage without saving to database"""
    try:
        home_html = await fetch_html(BASE_URL)
        all_post_urls = extract_all_post_urls(home_html)
        
        if not all_post_urls:
            return err("No posts found on homepage")
        
        posts = []
        for url in all_post_urls[:20]:  # Limit to 20 for performance
            post_id = extract_post_id(url)
            posts.append({
                "post_id": post_id,
                "post_url": url
            })
        
        return JSONResponse({
            "success": True,
            "total_posts": len(all_post_urls),
            "posts": posts
        })
    
    except Exception as e:
        return err(str(e))


@app.get("/saved")
async def get_saved_posts(limit: int = Query(50, ge=1, le=100)):
    """Get saved posts from MongoDB"""
    try:
        posts = collection.find(
            {}, 
            {"_id": 0}
        ).sort("scraped_at", -1).limit(limit).to_list()
        
        return JSONResponse({
            "success": True,
            "count": len(posts),
            "posts": posts
        })
    
    except Exception as e:
        return err(str(e))


@app.get("/health")
async def health():
    """Health check endpoint"""
    try:
        # Check MongoDB connection
        collection.find_one({})
        db_status = "connected"
    except:
        db_status = "disconnected"
    
    return {
        "status": "healthy",
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "mongodb": db_status,
        "base_url": BASE_URL
    }


# ============================================================
# RUN SERVER
# ============================================================

if __name__ == "__main__":
    import uvicorn
    uvicorn.run(app, host="0.0.0.0", port=8000)
