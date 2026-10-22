"""🦞 Research jobs: the Lobster executes, the Spider collects, the Elephant files.

  1. Plan     - the model proposes a few search phrases. Words only, cleaned by code.
  2. Collect  - the Spider turns them into requests to FIXED providers (Wikipedia, a news
                search for recent events). The model never picks a URL or a domain.
  3. Judge    - the Elephant keeps the articles that match the question best.
  4. Digest   - the Elephant keeps each article's own most relevant sentences.
  5. File     - the Elephant writes the brief, its cards, source pages and a map.

Every step is timed. /stop stops the running job between steps or pages, keeps what's
already stored, and cancels the waiting ones. The queue is append-only:
data/research_queue.jsonl.
"""
import json
import re
import threading

import elephant
import ollama
import shiba
import spider
from config import CHAT_KEEP_ALIVE, DATA_DIR, MODEL
from store import append_jsonl, read_jsonl, timed

QUEUE = DATA_DIR / "research_queue.jsonl"
MAX_SEARCHES = 4      # search phrases per job
MAX_CANDIDATES = 8    # articles collected and judged per job

STOP = threading.Event()


# --- Queue (append-only) ---------------------------------------------------------

def _records() -> list[dict]:
    return read_jsonl(QUEUE)


def enqueue(question: str) -> int:
    question = " ".join(question.split())
    for job in pending():                          # same question already waiting: reuse it
        if job["question"].lower() == question.lower():
            return job["id"]
    job_id = max((r["id"] for r in _records()), default=0) + 1
    append_jsonl(QUEUE, {"type": "queued", "id": job_id, "question": question})
    shiba.audit("research_queued", job=job_id, question=question)
    return job_id


def pending() -> list[dict]:
    records = _records()
    done = {r["id"] for r in records if r["type"] == "done"}
    return [r for r in records if r["type"] == "queued" and r["id"] not in done]


def mark_done(job_id: int, status: str, pages: int = 0, brief: str = "", topic: str = "", cards: int = 0) -> None:
    append_jsonl(QUEUE, {"type": "done", "id": job_id, "status": status, "pages": pages,
                         "brief": brief, "topic": topic, "cards": cards})


def history(n: int = 20) -> list[dict]:
    """Recent jobs, newest first."""
    records = _records()
    done: dict[int, dict] = {}
    for r in records:
        if r["type"] == "done":
            done.setdefault(r["id"], r)            # the first outcome recorded is the real one
    jobs = []
    for r in records:
        if r["type"] != "queued":
            continue
        d = done.get(r["id"], {})
        jobs.append({"id": r["id"], "question": r["question"], "queued": r.get("ts", ""),
                     "status": d.get("status", "waiting"), "finished": d.get("ts", ""),
                     "brief": d.get("brief", ""), "topic": d.get("topic", ""), "cards": d.get("cards", 0)})
    return jobs[::-1][:n]


def stop_all() -> int:
    """Stop the running job (between steps) and cancel the waiting ones. Returns how many."""
    waiting = pending()
    if waiting:
        STOP.set()
    for job in waiting:
        mark_done(job["id"], "stopped")
    if waiting:
        shiba.audit("research_stopped", jobs=len(waiting))
    return len(waiting)


# --- 1. Plan (model: words only) -------------------------------------------------

PLAN_SYSTEM = """You plan research for a personal knowledge base.
Given a question, return 2 to 3 short encyclopedia search phrases (2 to 6 words
each) that stay strictly on the question. For a broad topic, include an overview
or "list of" article. Do not drift to related people or works the question did
not ask about. Set needs_news to true only if the question is about recent or
current events. Return JSON only."""

PLAN_SCHEMA = {
    "type": "object",
    "properties": {"searches": {"type": "array", "items": {"type": "string"}},
                   "needs_news": {"type": "boolean"}},
    "required": ["searches", "needs_news"],
}


def clean_phrase(text: str) -> str:
    """Letters, digits, spaces, apostrophes and hyphens only; no links survive this."""
    text = re.sub(r"https?://\S+|www\.\S+", " ", text)
    text = re.sub(r"[^\w\s'\-]", " ", text)
    return re.sub(r"\s+", " ", text).strip()[:80]


def plan(question: str) -> dict:
    searches, needs_news = [], False
    try:
        reply = ollama.chat(MODEL, [{"role": "system", "content": PLAN_SYSTEM},
                                    {"role": "user", "content": question}],
                            schema=PLAN_SCHEMA, keep_alive=CHAT_KEEP_ALIVE)
        data = json.loads(reply)
        searches = [clean_phrase(s) for s in data.get("searches", []) if isinstance(s, str)]
        needs_news = data.get("needs_news") is True
    except Exception:
        pass                                     # model away: the question itself is searched
    # The question itself always goes first: on Wikipedia it usually finds the overview article.
    searches = list(dict.fromkeys(s for s in [clean_phrase(question)] + searches if s))[:MAX_SEARCHES]
    return {"searches": searches, "needs_news": needs_news}


# --- The whole job ----------------------------------------------------------------

def run_job(job: dict, say=lambda text: None, progress=None) -> dict:
    """Run one job start to finish. say(text) reports milestones; progress(i, n, url) reports
    pages. Returns {"status": done | nothing | offline | stopped | paused, ...}."""
    if shiba.paused():
        return {"status": "paused"}
    q = job["question"]
    with timed("research"):
        with timed("plan"):
            p = plan(q)
        if STOP.is_set():
            return {"status": "stopped"}
        say(f"🕷️ Searching: {', '.join(p['searches'])}")
        with timed("collect"):
            got = spider.wikipedia(p["searches"], MAX_CANDIDATES, should_stop=STOP.is_set)
            kept, skipped = elephant.rank(q, got["articles"])
            urls = []
            for a in kept:
                spider.to_inbox({"url": a["url"], "title": a["title"], "fetched_at": a["fetched_at"],
                                 "content_type": "text/plain", "content": f"{a['title']}. {a['text']}",
                                 "sha256": a["sha256"]}, origin="research", job=job["id"], question=q)
                urls.append(a["url"])
            if p["needs_news"] and len(urls) < elephant.MAX_PAGES:
                try:
                    page = spider.news(clean_phrase(q))
                    spider.to_inbox(page, origin="research", job=job["id"], question=q)
                    urls.append(page["url"])
                except Exception as e:
                    got["failed"].append(f"news: {type(e).__name__}")
        shiba.audit("research_gathered", job=job["id"], pages=len(urls), skipped=len(skipped),
                    failed=len(got["failed"]))
        if STOP.is_set():
            return {"status": "stopped"}
        if not urls:
            if got["network_errors"] and got["network_errors"] == len(p["searches"]):
                return {"status": "offline"}          # stays queued until the tablet is back online
            mark_done(job["id"], "nothing found")
            return {"status": "nothing", "failed": got["failed"]}
        skipped_note = f" Skipped as off-topic: {', '.join(a['title'] for a in skipped)}." if skipped else ""
        say(f"📚 Kept {len(urls)} article{'s' if len(urls) != 1 else ''}.{skipped_note} Reading them now.")
        r = elephant.digest(job=job["id"], progress=progress, should_stop=STOP.is_set, unload=False)
        if r["stopped"] or STOP.is_set():
            return {"status": "stopped", "facts": r["facts"]}
        b = elephant.brief(job, urls)
    mark_done(job["id"], "done", len(urls), brief=b["text"], topic=b["topic"], cards=b["cards"])
    return {"status": "done", "brief": b["text"], "facts": r["facts"], "rejected": r["rejected"],
            "suspicious": r["suspicious"]}
