"""🕷️ The Spider: fetches the pages you listed and saves them, untouched.

It has no model, makes no decisions, and never reads what it downloads.
Its only output is files in cache/inbox/ for the Elephant to digest.

Run by hand:  python spider.py
"""
import hashlib
import json
import time
import urllib.request
from urllib.parse import urlparse

from config import CACHE_DIR, PROXY, SOURCES_FILE

MAX_BYTES = 2_000_000
INBOX = CACHE_DIR / "inbox"
SEEN = CACHE_DIR / "seen.json"   # url -> hash of last download, to skip unchanged pages


def _opener():
    proxies = {"http": PROXY, "https": PROXY} if PROXY else {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies))
    opener.addheaders = [("User-Agent", "Mozilla/5.0 (Lobster Spider; personal use)")]
    return opener


def load_sources() -> list[str]:
    if not SOURCES_FILE.exists():
        return []
    return [l.strip() for l in SOURCES_FILE.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.lstrip().startswith("#")]


def add_source(url: str) -> str:
    """Only called from your own /addsource command, never by the model."""
    url = url.strip()
    if urlparse(url).scheme not in ("http", "https"):
        return "Only http:// or https:// links."
    sources = load_sources()
    if url in sources:
        return "Already watching that."
    SOURCES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with SOURCES_FILE.open("a", encoding="utf-8") as f:
        f.write(url + "\n")
    return f"🕷️ Watching {url}"


def fetch(url: str, opener) -> dict:
    with opener.open(url, timeout=30) as r:
        raw = r.read(MAX_BYTES + 1)
        ctype = r.headers.get("Content-Type", "")
        charset = r.headers.get_content_charset() or "utf-8"
    if len(raw) > MAX_BYTES:
        raw = raw[:MAX_BYTES]
    return {
        "url": url,
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "content_type": ctype,
        "content": raw.decode(charset, errors="replace"),
        "sha256": hashlib.sha256(raw).hexdigest(),
    }


def crawl() -> dict:
    INBOX.mkdir(parents=True, exist_ok=True)
    seen = json.loads(SEEN.read_text()) if SEEN.exists() else {}
    opener = _opener()
    report = {"new": 0, "unchanged": 0, "failed": []}
    for url in load_sources():
        if urlparse(url).scheme not in ("http", "https"):
            report["failed"].append(f"{url} (bad scheme)")
            continue
        try:
            page = fetch(url, opener)
        except Exception as e:
            report["failed"].append(f"{url} ({type(e).__name__})")
            continue
        if seen.get(url) == page["sha256"]:
            report["unchanged"] += 1
            continue
        name = f"{int(time.time())}-{hashlib.sha1(url.encode()).hexdigest()[:10]}.json"
        (INBOX / name).write_text(json.dumps(page, ensure_ascii=False), encoding="utf-8")
        seen[url] = page["sha256"]
        report["new"] += 1
    SEEN.write_text(json.dumps(seen, indent=1))
    return report


def inbox_count() -> int:
    return len(list(INBOX.glob("*.json"))) if INBOX.exists() else 0


if __name__ == "__main__":
    print(crawl())
