"""🕷️ The Spider: the collector. It fetches; it never thinks.

It has no model and never follows a link it found inside a page. It collects
only two things:
  - research material from FIXED providers (Wikipedia's API, a news search),
    for search words the Lobster gives it; it never chooses a site itself
  - documents you asked it to /save, kept with a receipt

Every link passes Shiba first (no local or private addresses, checked again on
every redirect). It respects robots.txt for ordinary pages, waits between
requests to the same site, and retries politely.
"""
import hashlib
import json
import re
import time
import urllib.error
import urllib.request
import urllib.robotparser
from urllib.parse import quote, unquote, urlencode, urlparse

import shiba
from config import CACHE_DIR, CONTACT, LIBRARY_DIR, PROXY
from store import write_atomic

MAX_BYTES = 2_000_000          # pages: anything longer is cut
MAX_DOC_BYTES = 50_000_000     # /save documents: anything longer is refused
MAX_REDIRECTS = 3
DOMAIN_GAP = 10.0              # seconds between requests to the same ordinary site
API_GAP = 1.0                  # official APIs built for programs (e.g. Wikipedia's) need less
RETRIES = 2
RETRY_BASE = 2.0               # seconds; doubles each retry
TIMEOUT = 30
AGENT = "LobsterSpider"
USER_AGENT = f"{AGENT}/1.0 ({CONTACT})"   # honest name + contact, as Wikimedia's policy asks

PAGE_TYPES = ("text/html", "application/xhtml+xml", "application/xml", "text/xml",
              "application/rss+xml", "application/atom+xml", "application/rdf+xml",
              "application/json", "text/plain")
DOC_TYPES = {"application/pdf": ".pdf", "text/html": ".html", "text/plain": ".txt",
             "text/csv": ".csv", "application/json": ".json", "application/xml": ".xml"}

INBOX = CACHE_DIR / "inbox"

PROVIDERS = {                       # the only places research ever reads from
    "wikipedia": "https://en.wikipedia.org/w/api.php",
    "wikipedia_page": "https://en.wikipedia.org/wiki/",
    "news": "https://news.google.com/rss/search",
}
PER_SEARCH = 3                      # search results looked at per phrase


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


def _wait_turn(host: str, gap: float | None = None) -> None:
    gap = (DOMAIN_GAP if gap is None else gap) - (time.time() - _last_hit.get(host, 0.0))
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
         headers: dict | None = None, robots: bool = True, gap: float | None = None) -> dict:
    """Download one URL under every rule. Returns bytes plus metadata."""
    problem = shiba.url_problem(url)
    if problem:
        shiba.audit("fetch_refused", url=url, reason=problem)
        raise Refused(problem)
    if robots and not robots_allows(url, opener):
        shiba.audit("fetch_refused", url=url, reason="robots.txt")
        raise Refused("the site's robots.txt asks crawlers not to fetch it")
    _wait_turn(urlparse(url).hostname or "", gap)
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


def fetch(url: str, opener, headers: dict | None = None, robots: bool = True, gap: float | None = None) -> dict:
    """Fetch a page (HTML, feed, JSON or text) and return it decoded."""
    got = _get(url, opener, kinds=PAGE_TYPES, limit=MAX_BYTES, truncate=True,
               headers=headers, robots=robots, gap=gap)
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


def to_inbox(page: dict, origin: str, job: int | None = None, question: str = "") -> None:
    """Hand a page to the Elephant, untouched."""
    INBOX.mkdir(parents=True, exist_ok=True)
    record = dict(page, origin=origin, job=job, question=question)
    name = f"{time.time_ns()}-{hashlib.sha1(page['url'].encode()).hexdigest()[:10]}.json"
    write_atomic(INBOX / name, json.dumps(record, ensure_ascii=False))


def inbox_count() -> int:
    return len(list(INBOX.glob("*.json"))) if INBOX.exists() else 0


# --- Research collecting (fixed providers only) -------------------------------------

def _wiki_api(params: dict) -> str:
    return PROVIDERS["wikipedia"] + "?" + urlencode(dict(params, format="json", formatversion=2))


def wikipedia(phrases: list[str], limit: int, should_stop=None) -> dict:
    """Search Wikipedia for each phrase and fetch the articles' plain text.

    Returns {"articles": [{"title", "url", "text", "fetched_at", "sha256"}], "failed": [...],
             "network_errors": n}. The phrases are words only; URLs are built here, by code.
    """
    opener = _opener()
    articles, failed, seen, network_errors = [], [], set(), 0
    for phrase in phrases:
        if len(articles) >= limit or (should_stop and should_stop()):
            break
        try:
            hits = json.loads(fetch(_wiki_api({"action": "query", "list": "search", "srsearch": phrase,
                                               "srlimit": PER_SEARCH}), opener, robots=False,
                                    gap=API_GAP)["content"])
            titles = [h["title"] for h in hits.get("query", {}).get("search", [])
                      if isinstance(h, dict) and isinstance(h.get("title"), str)]
        except Refused as e:
            failed.append(f"search '{phrase}': {e}")
            continue
        except Exception as e:
            network_errors += 1
            failed.append(f"search '{phrase}': {type(e).__name__}")
            continue
        for title in titles:
            if len(articles) >= limit or title in seen:
                continue
            seen.add(title)
            try:
                page = fetch(_wiki_api({"action": "query", "prop": "extracts", "explaintext": 1,
                                        "redirects": 1, "titles": title}), opener, robots=False, gap=API_GAP)
                article = json.loads(page["content"])["query"]["pages"][0]
                text = article.get("extract") or ""
            except Exception as e:
                failed.append(f"article '{title}': {type(e).__name__}")
                continue
            if text.strip():
                articles.append({"title": article["title"], "text": text,
                                 "url": PROVIDERS["wikipedia_page"] + quote(article["title"].replace(" ", "_")),
                                 "fetched_at": page["fetched_at"], "sha256": page["sha256"]})
    return {"articles": articles, "failed": failed, "network_errors": network_errors}


def news(phrase: str) -> dict:
    """The news search feed for one phrase (only for questions about recent events)."""
    url = PROVIDERS["news"] + "?" + urlencode({"q": phrase, "hl": "en-US", "gl": "US", "ceid": "US:en"})
    return fetch(url, _opener(), robots=False, gap=API_GAP)


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
    write_atomic(path, got["raw"])
    receipt = {"file": path.name, "url": url, "final_url": got["final_url"],
               "fetched_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
               "content_type": got["content_type"], "bytes": len(got["raw"]), "sha256": digest}
    write_atomic(LIBRARY_DIR / f"{path.name}.receipt.json", json.dumps(receipt, indent=1))
    shiba.audit("document_saved", url=url, file=path.name, bytes=len(got["raw"]))

    if got["content_type"] == "application/pdf":
        text = _pdf_text(path)
    else:
        text = got["raw"].decode(got["charset"], errors="replace")
    size = f"{len(got['raw']) / 1_000_000:.1f} MB"
    if text:
        to_inbox({"url": url, "fetched_at": receipt["fetched_at"], "title": path.name,
                  "content_type": "text/plain" if ext == ".pdf" else got["content_type"],
                  "content": text, "sha256": digest}, origin="document")
        return f"📄 Saved {path.name} ({size}) to the Library. The Elephant will index its text for /ask."
    note = " Install pypdf (pip install pypdf) to index PDF text." if ext == ".pdf" else ""
    return f"📄 Saved {path.name} ({size}) to the Library.{note}"
