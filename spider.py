"""🕷️ The Spider: fetches, never thinks.

It has no model and never follows a link it found inside a page. It fetches
only: the sources you added, documents you asked it to /save, and search
results from the fixed research providers in research.py.

Every link passes Shiba first (no local or private addresses, checked again
on every redirect). It respects robots.txt for crawling, waits between
requests to the same site, retries politely, and skips unchanged pages.

Run by hand:  python spider.py
"""
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
import urllib.robotparser
from urllib.parse import unquote, urlparse

import shiba
from config import CACHE_DIR, DATA_DIR, LIBRARY_DIR, PROXY, SOURCES_FILE

MAX_BYTES = 2_000_000          # pages: anything longer is cut
MAX_DOC_BYTES = 50_000_000     # /save documents: anything longer is refused
MAX_REDIRECTS = 3
DOMAIN_GAP = 10.0              # seconds between requests to the same site
RETRIES = 2
RETRY_BASE = 2.0               # seconds; doubles each retry
TIMEOUT = 30
AGENT = "LobsterSpider"
USER_AGENT = f"Mozilla/5.0 (compatible; {AGENT}/1.0; personal research)"

PAGE_TYPES = ("text/html", "application/xhtml+xml", "application/xml", "text/xml",
              "application/rss+xml", "application/atom+xml", "application/rdf+xml",
              "application/json", "text/plain")
DOC_TYPES = {"application/pdf": ".pdf", "text/html": ".html", "text/plain": ".txt",
             "text/csv": ".csv", "application/json": ".json", "application/xml": ".xml"}

INBOX = CACHE_DIR / "inbox"
SEEN = CACHE_DIR / "seen.json"      # url -> sha256, etag, last-modified of the last download
SOURCES_ARCHIVE = DATA_DIR / "sources-archive.txt"


class Refused(Exception):
    """The Spider (or Shiba) declined to fetch this."""


# --- The guarded opener --------------------------------------------------------

class _GuardedRedirect(urllib.request.HTTPRedirectHandler):
    max_redirections = MAX_REDIRECTS

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        problem = shiba.url_problem(newurl)
        if problem:
            shiba.audit("redirect_blocked", url=newurl, reason=problem)
            raise Refused(f"redirect blocked: {problem}")
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener():
    proxies = {"http": PROXY, "https": PROXY} if PROXY else {}
    opener = urllib.request.build_opener(urllib.request.ProxyHandler(proxies), _GuardedRedirect())
    opener.addheaders = [("User-Agent", USER_AGENT)]
    return opener


# --- Politeness ------------------------------------------------------------------

_last_hit: dict[str, float] = {}
_robots: dict[str, urllib.robotparser.RobotFileParser] = {}


def _wait_turn(host: str) -> None:
    gap = DOMAIN_GAP - (time.time() - _last_hit.get(host, 0.0))
    if gap > 0:
        time.sleep(gap)
    _last_hit[host] = time.time()


def robots_allows(url: str, opener) -> bool:
    p = urlparse(url)
    base = f"{p.scheme}://{p.netloc}"
    rp = _robots.get(base)
    if rp is None:
        rp = urllib.robotparser.RobotFileParser()
        try:
            with opener.open(base + "/robots.txt", timeout=15) as r:
                rp.parse(r.read(200_000).decode("utf-8", "replace").splitlines())
        except urllib.error.HTTPError as e:
            if e.code in (401, 403):
                rp.disallow_all = True
            else:
                rp.allow_all = True
        except Exception:
            rp.allow_all = True
        _robots[base] = rp
    return rp.can_fetch(AGENT, url)


# --- Fetching --------------------------------------------------------------------

def _get(url: str, opener, *, kinds, limit: int, truncate: bool,
         headers: dict | None = None, robots: bool = True) -> dict:
    """Download one URL under every rule. Returns bytes plus metadata."""
    problem = shiba.url_problem(url)
    if problem:
        shiba.audit("fetch_refused", url=url, reason=problem)
        raise Refused(problem)
    if robots and not robots_allows(url, opener):
        shiba.audit("fetch_refused", url=url, reason="robots.txt")
        raise Refused("the site's robots.txt asks crawlers not to fetch it")
    _wait_turn(urlparse(url).hostname or "")
    req = urllib.request.Request(url, headers=headers or {})
    for attempt in range(RETRIES + 1):
        try:
            with opener.open(req, timeout=TIMEOUT) as r:
                ctype = r.headers.get("Content-Type", "").split(";")[0].strip().lower()
                if not ctype.startswith(tuple(kinds)):
                    shiba.audit("fetch_refused", url=url, reason=f"type {ctype or 'unknown'}")
                    raise Refused(f"content type {ctype or 'unknown'} is not allowed")
                raw = r.read(limit + 1)
                if len(raw) > limit:
                    if not truncate:
                        shiba.audit("fetch_refused", url=url, reason="too large")
                        raise Refused(f"it is larger than {limit // 1_000_000} MB")
                    raw = raw[:limit]
                return {"raw": raw, "content_type": ctype, "final_url": r.geturl(),
                        "charset": r.headers.get_content_charset() or "utf-8",
                        "etag": r.headers.get("ETag"), "last_modified": r.headers.get("Last-Modified")}
        except urllib.error.HTTPError as e:
            if e.code == 304:
                return {"not_modified": True}
            if e.code in (429, 500, 502, 503, 504) and attempt < RETRIES:
                time.sleep(RETRY_BASE * 2 ** attempt)
                continue
            raise
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt < RETRIES:
                time.sleep(RETRY_BASE * 2 ** attempt)
                continue
            raise
    raise Refused("gave up after retries")


def fetch(url: str, opener, headers: dict | None = None, robots: bool = True) -> dict:
    """Fetch a page (HTML, feed, JSON or text) and return it decoded."""
    got = _get(url, opener, kinds=PAGE_TYPES, limit=MAX_BYTES, truncate=True,
               headers=headers, robots=robots)
    if got.get("not_modified"):
        return got
    return {
        "url": url,
        "final_url": got["final_url"],
        "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "content_type": got["content_type"],
        "content": got["raw"].decode(got["charset"], errors="replace"),
        "sha256": hashlib.sha256(got["raw"]).hexdigest(),
        "etag": got["etag"],
        "last_modified": got["last_modified"],
    }


def to_inbox(page: dict, origin: str, job: int | None = None) -> None:
    """Hand a page to the Elephant, untouched."""
    INBOX.mkdir(parents=True, exist_ok=True)
    record = dict(page, origin=origin, job=job)
    name = f"{time.time_ns()}-{hashlib.sha1(page['url'].encode()).hexdigest()[:10]}.json"
    tmp = INBOX / (name + ".tmp")
    tmp.write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
    tmp.replace(INBOX / name)


# --- Your source list --------------------------------------------------------------

def load_sources() -> list[str]:
    if not SOURCES_FILE.exists():
        return []
    return [l.strip() for l in SOURCES_FILE.read_text(encoding="utf-8").splitlines()
            if l.strip() and not l.lstrip().startswith("#")]


def add_source(url: str) -> str:
    """Only called from your own /addsource command, never by the model."""
    url = url.strip()
    problem = shiba.url_problem(url, allow_unresolved=True)
    if problem:
        shiba.audit("source_refused", url=url, reason=problem)
        return f"❌ Not added: {problem}."
    if url in load_sources():
        return "Already watching that."
    SOURCES_FILE.parent.mkdir(parents=True, exist_ok=True)
    with SOURCES_FILE.open("a", encoding="utf-8") as f:
        f.write(url + "\n")
    shiba.audit("source_added", url=url)
    return f"🕷️ Watching {url}"


def remove_source(url: str) -> str:
    """Stops watching a source. The line is archived, never deleted."""
    url = url.strip()
    sources = load_sources()
    if url not in sources:
        return "I'm not watching that."
    with SOURCES_ARCHIVE.open("a", encoding="utf-8") as f:
        f.write(f"{time.strftime('%Y-%m-%d %H:%M')} {url}\n")
    lines = [l for l in SOURCES_FILE.read_text(encoding="utf-8").splitlines() if l.strip() != url]
    tmp = SOURCES_FILE.with_suffix(".tmp")
    tmp.write_text("\n".join(lines) + "\n", encoding="utf-8")
    tmp.replace(SOURCES_FILE)
    shiba.audit("source_removed", url=url)
    return f"🕷️ Stopped watching {url} (kept in sources-archive.txt)."


def crawl() -> dict:
    INBOX.mkdir(parents=True, exist_ok=True)
    seen = json.loads(SEEN.read_text()) if SEEN.exists() else {}
    opener = _opener()
    report = {"new": 0, "unchanged": 0, "failed": []}
    for url in load_sources():
        prev = seen.get(url)
        if isinstance(prev, str):                 # older format: just the hash
            prev = {"sha256": prev}
        headers = {}
        if prev and prev.get("etag"):
            headers["If-None-Match"] = prev["etag"]
        if prev and prev.get("last_modified"):
            headers["If-Modified-Since"] = prev["last_modified"]
        try:
            page = fetch(url, opener, headers=headers)
        except Refused as e:
            report["failed"].append(f"{url} (refused: {e})")
            continue
        except Exception as e:
            report["failed"].append(f"{url} ({type(e).__name__})")
            continue
        if page.get("not_modified") or (prev and prev.get("sha256") == page["sha256"]):
            report["unchanged"] += 1
            continue
        to_inbox(page, origin="source")
        seen[url] = {"sha256": page["sha256"], "etag": page["etag"], "last_modified": page["last_modified"]}
        report["new"] += 1
    SEEN.parent.mkdir(parents=True, exist_ok=True)
    SEEN.write_text(json.dumps(seen, indent=1))
    return report


def inbox_count() -> int:
    return len(list(INBOX.glob("*.json"))) if INBOX.exists() else 0


# --- Saving documents (/save) ------------------------------------------------------

def _doc_name(url: str, ext: str) -> str:
    stem = unquote(urlparse(url).path.rstrip("/").rsplit("/", 1)[-1]) or urlparse(url).hostname or "document"
    stem = re.sub(r"\.[A-Za-z0-9]{1,5}$", "", stem)
    stem = re.sub(r"[^A-Za-z0-9._-]+", "-", stem).strip("-.")[:60] or "document"
    return f"{time.strftime('%Y-%m-%d')}-{stem}{ext}"


def _pdf_text(path) -> str | None:
    try:
        from pypdf import PdfReader          # optional: pip install pypdf
    except ImportError:
        return None
    import logging
    logging.getLogger("pypdf").setLevel(logging.ERROR)
    try:
        text = []
        for page in PdfReader(str(path)).pages:
            text.append(page.extract_text() or "")
            if sum(map(len, text)) > 40_000:
                break
        return "\n".join(text)
    except Exception:
        return None


def save_document(url: str) -> str:
    """Download a document you asked for into the Library, with a receipt file.

    Only called from your own /save command, never by the model.
    """
    url = url.strip()
    try:
        got = _get(url, _opener(), kinds=DOC_TYPES, limit=MAX_DOC_BYTES, truncate=False)
    except Refused as e:
        return f"❌ Not saved: {e}."
    except Exception as e:
        return f"❌ Not saved: {type(e).__name__}."
    digest = hashlib.sha256(got["raw"]).hexdigest()
    LIBRARY_DIR.mkdir(parents=True, exist_ok=True)
    for receipt in LIBRARY_DIR.glob("*.receipt.json"):
        try:
            if json.loads(receipt.read_text(encoding="utf-8")).get("sha256") == digest:
                return f"Already saved as {receipt.name.removesuffix('.receipt.json')}."
        except json.JSONDecodeError:
            continue
    ext = DOC_TYPES.get(got["content_type"], ".bin")
    name = _doc_name(url, ext)
    path, n = LIBRARY_DIR / name, 2
    while path.exists():                              # never overwrite
        path = LIBRARY_DIR / name.replace(ext, f"-{n}{ext}")
        n += 1
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_bytes(got["raw"])
    tmp.replace(path)
    receipt = {"file": path.name, "url": url, "final_url": got["final_url"],
               "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "content_type": got["content_type"], "bytes": len(got["raw"]), "sha256": digest}
    (LIBRARY_DIR / f"{path.name}.receipt.json").write_text(json.dumps(receipt, indent=1), encoding="utf-8")
    shiba.audit("document_saved", url=url, file=path.name, bytes=len(got["raw"]))

    if got["content_type"] == "application/pdf":
        text = _pdf_text(path)
    else:
        text = got["raw"].decode(got["charset"], errors="replace")
    size = f"{len(got['raw']) / 1_000_000:.1f} MB"
    if text:
        to_inbox({"url": url, "fetched_at": receipt["fetched_at"],
                  "content_type": "text/plain" if ext == ".pdf" else got["content_type"],
                  "content": text, "sha256": digest}, origin="document")
        return f"📄 Saved {path.name} ({size}) to the Library. Its text is queued for the Elephant."
    note = " Install pypdf (pip install pypdf) to index PDF text." if ext == ".pdf" else ""
    return f"📄 Saved {path.name} ({size}) to the Library.{note}"


if __name__ == "__main__":
    print(crawl())
