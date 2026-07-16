from fastapi import FastAPI, Query
from fastapi.responses import JSONResponse
from fastapi.middleware.cors import CORSMiddleware
import httpx
import re
from datetime import datetime, timezone
from typing import Optional

app = FastAPI()

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)

BASE_URL = "https://new3.hdhub4u.cl/"

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


def err(message: str, status: int = 500, **extra):
    return JSONResponse({"success": False, "error": message, **extra}, status_code=status)


async def fetch_html(url: str) -> str:
    async with httpx.AsyncClient(
        headers=HEADERS,
        follow_redirects=True,
        timeout=30,
        verify=False,  # SSL issues bypass
    ) as client:
        response = await client.get(url)
        response.raise_for_status()
        return response.text


# ── Scraping Logic (same as CF Worker) ──

def extract_post_url(home_html: str) -> Optional[str]:
    patterns = [
        r'class="recent-movies"[\s\S]*?<a\s+href="(https?://[^"]+)"',
        r'class="thumb[^"]*"[\s\S]*?<a\s+href="(https?://new1\.hdhub4u\.cl/[^"]+)"',
        r'<figure>[\s\S]*?<a\s+href="(https?://new1\.hdhub4u\.cl/[^"]+)"',
        r'href="(https?://new[0-9]*\.hdhub4u\.cl/[a-z0-9][a-z0-9-]{5,}/?)"',
    ]
    for pattern in patterns:
        match = re.search(pattern, home_html)
        if match:
            url = match.group(1)
            if (
                "/category/" not in url
                and "/page/" not in url
                and not url.endswith(".cl/")
            ):
                return url
    return None


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


# ── Routes ──

@app.get("/latest")
async def latest(post: Optional[str] = Query(None)):
    try:
        post_url = post

        # STEP 1: Homepage → Latest post URL
        if not post_url:
            home_html = await fetch_html(BASE_URL)
            post_url = extract_post_url(home_html)

            if not post_url:
                return err(
                    "Latest post link not found",
                    debug=home_html[:500]
                )

        # STEP 2: Post page → Scrape data
        post_html = await fetch_html(post_url)

        return JSONResponse({
            "success": True,
            "scraped_at": datetime.now(timezone.utc).isoformat(),
            "post_url": post_url,
            "title": extract_title(post_html),
            "thumbnail": extract_thumbnail(post_html),
            "info": extract_info(post_html),
            "download_links": extract_links(post_html),
        })

    except httpx.HTTPStatusError as e:
        return err(f"HTTP {e.response.status_code}: {e.request.url}")
    except httpx.TimeoutException:
        return err("Request timed out")
    except Exception as e:
        return err(str(e))


@app.get("/")
async def root():
    return {"status": "ok", "endpoints": ["/latest", "/latest?post=<url>"]}
