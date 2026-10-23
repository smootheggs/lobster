"""🦞 Research jobs: Sloth Lobster executes, the Spider collects, the Elephant files.

Two kinds of job, both outline-first:

  Quick (/research)  1 round: plan 2-3 sub-questions, collect, write. A few minutes.
  Deep  (/deep)      You OK a plan of 3-5 sub-questions first. It runs overnight (DEEP_HOURS)
                     with the bigger writer: 1 broad round, then up to 2 rounds that fill only
                     the sections code finds thin, within DEEP_MAX_MINUTES. It saves its place
                     after every round, so a restart, an offline spell or a quick job in
                     between doesn't lose anything.

  1. Plan     - the overview article's headings are read by code; the model turns the question
                into sub-questions and search words. Words only, cleaned by code.
  2. Collect  - the Spider turns them into requests to FIXED providers (Wikipedia, a news
                search for recent events). The model never picks a URL or a domain.
  3. Judge    - articles off the question are skipped; each sub-question keeps its best few.
  4. Digest   - the Elephant keeps each article's own sentences that answer the question or
                one of its sub-questions.
  5. Write    - per sub-question: a short answer and its cards; then the bottom line. Every
                sentence is checked against the facts it cites; failures are dropped.

Every step is timed. /stop stops the running job between steps or pages, keeps what's
already stored, and cancels the waiting ones. The queue is append-only:
data/research_queue.jsonl.
"""
import json
import threading
import time

import elephant
import ollama
import outline
import shiba
import spider
from config import (DATA_DIR, DEEP_HOURS, DEEP_MAX_MINUTES, DEEP_WRITER_MODEL, MODEL, WRITER_MODEL)
from store import append_jsonl, read_jsonl, timed, write_atomic

QUEUE = DATA_DIR / "research_queue.jsonl"
DEEP_DIR = DATA_DIR / "deep"          # a deep job's place, saved after every round
MAX_SEARCHES = 4      # quick jobs: search phrases (the question plus one per sub-question)
MAX_CANDIDATES = 8    # articles collected per search batch

STOP = threading.Event()
clean_phrase = outline.clean_phrase


# --- Queue (append-only) ---------------------------------------------------------

def _records() -> list[dict]:
    return read_jsonl(QUEUE)


def _jobs(records: list[dict] | None = None) -> dict[int, dict]:
    """Every job with its latest plan and approval merged in. An approval only counts if it
    came after the latest plan (changing the plan asks for your OK again)."""
    jobs: dict[int, dict] = {}
    for r in records if records is not None else _records():
        jid = r.get("id")
        if r.get("type") == "queued":
            jobs[jid] = {"id": jid, "question": r["question"], "mode": r.get("mode", "quick"), "queued": r.get("ts", ""),
                         "plan": None, "approved": None, "done": None}
        elif jid in jobs:
            j = jobs[jid]
            if r["type"] == "plan":
                j["plan"], j["approved"] = {"sections": r["sections"], "needs_news": r.get("needs_news", False),
                                            "by": r.get("by", "lobster")}, None
            elif r["type"] == "approved" and j["plan"] is not None:
                j["approved"] = r.get("when", "night")
            elif r["type"] == "done" and j["done"] is None:
                j["done"] = r                   # the first outcome recorded is the real one
    return jobs


def enqueue(question: str, mode: str = "quick") -> int:
    question = " ".join(question.split())
    mode = "deep" if mode == "deep" else "quick"
    for job in pending():                          # same question already waiting: reuse it
        if job["question"].lower() == question.lower() and job["mode"] == mode:
            return job["id"]
    job_id = max((r["id"] for r in _records()), default=0) + 1
    append_jsonl(QUEUE, {"type": "queued", "id": job_id, "question": question, "mode": mode})
    shiba.audit("research_queued", job=job_id, question=question, mode=mode)
    return job_id


def pending() -> list[dict]:
    return [j for j in _jobs().values() if j["done"] is None]


def job(job_id: int) -> dict | None:
    return _jobs().get(job_id)


def set_plan(job_id: int, sections: list[dict], needs_news: bool = False, by: str = "lobster") -> None:
    append_jsonl(QUEUE, {"type": "plan", "id": job_id, "sections": sections, "needs_news": needs_news, "by": by})
    shiba.audit("research_planned", job=job_id, by=by, sections=[s["question"] for s in sections])


def approve(job_id: int, when: str) -> None:
    append_jsonl(QUEUE, {"type": "approved", "id": job_id, "when": "now" if when == "now" else "night"})
    shiba.audit("research_approved", job=job_id, when=when)


def mark_done(job_id: int, status: str, pages: int = 0, brief: str = "", topic: str = "", cards: int = 0,
              metrics: dict | None = None) -> None:
    append_jsonl(QUEUE, {"type": "done", "id": job_id, "status": status, "pages": pages,
                         "brief": brief, "topic": topic, "cards": cards, "metrics": metrics or {}})


def stage(j: dict) -> str:
    """Where a waiting job stands, in words the app shows."""
    if j["mode"] != "deep":
        return "waiting"
    if j["plan"] is None:
        return "planning"
    if j["approved"] is None:
        return "needs your OK"
    return "next" if j["approved"] == "now" else "tonight"


def history(n: int = 20) -> list[dict]:
    """Recent jobs, newest first."""
    out = []
    for j in _jobs().values():
        d = j["done"] or {}
        out.append({"id": j["id"], "question": j["question"], "queued": j["queued"], "mode": j["mode"],
                    "status": d.get("status", "waiting"), "finished": d.get("ts", ""),
                    "stage": "" if d else stage(j),
                    "plan": [s["question"] for s in (j["plan"] or {}).get("sections", [])],
                    "brief": d.get("brief", ""), "topic": d.get("topic", ""), "cards": d.get("cards", 0),
                    "metrics": d.get("metrics", {})})
    return out[::-1][:n]


def stop_all() -> int:
    """Stop the running job (between steps) and cancel the waiting ones. Returns how many."""
    waiting = pending()
    if waiting:
        STOP.set()
    for j in waiting:
        mark_done(j["id"], "stopped")
    if waiting:
        shiba.audit("research_stopped", jobs=len(waiting))
    return len(waiting)


def cancel(job_id: int) -> bool:
    j = job(job_id)
    if j is None or j["done"] is not None:
        return False
    mark_done(job_id, "cancelled")
    shiba.audit("research_cancelled", job=job_id)
    return True


# --- When deep jobs run ------------------------------------------------------------------

def _hours(spec: str = DEEP_HOURS) -> tuple[int, int]:
    try:
        a, b = (int(x) % 24 for x in spec.split("-", 1))
        return a, b
    except (ValueError, AttributeError):
        return 23, 6


def in_window(hour: int, spec: str = DEEP_HOURS) -> bool:
    a, b = _hours(spec)
    return a <= hour < b if a < b else (hour >= a or hour < b)


def window_text(spec: str = DEEP_HOURS) -> str:
    a, b = _hours(spec)
    return f"{a:02d}:00–{b:02d}:00"


def next_job(hour: int | None = None, charging: bool | None = None) -> tuple[dict | None, str]:
    """(job, "run" | "plan"), or (None, why nothing can run now). Quick jobs first; deep jobs
    get a plan straight away, then wait for your OK, then for the night window and the charger."""
    hour = time.localtime().tm_hour if hour is None else hour
    waiting = pending()
    for j in waiting:
        if j["mode"] != "deep":
            return j, "run"
    for j in waiting:
        if j["plan"] is None:
            return j, "plan"
    why = ""
    for j in waiting:
        if j["approved"] == "now":
            return j, "run"
        if j["approved"] == "night":
            if not in_window(hour):
                why = why or f"🌙 Deep research #{j['id']} waits for tonight ({window_text()})"
            elif charging is False:
                why = why or f"🔌 Deep research #{j['id']} waits for the charger"
            else:
                return j, "run"
        elif not why:
            why = f"🐘 Deep research #{j['id']} waits for your OK on its plan"
    return None, why


def waiting_now(hour: int | None = None) -> int:
    """Jobs that are actually in line right now (not deep jobs waiting for the night or your OK)."""
    hour = time.localtime().tm_hour if hour is None else hour
    return sum(1 for j in pending() if j["mode"] != "deep" or j["plan"] is None or j["approved"] == "now"
               or (j["approved"] == "night" and in_window(hour)))


# --- 1. Plan (model: words only) -------------------------------------------------

def _budget(mode: str) -> dict:
    return outline.DEEP if mode == "deep" else outline.QUICK


def plan(question: str, heads: list[str] | tuple = (), mode: str = "quick") -> dict:
    """{"sections": [{"question", "searches"}], "needs_news", "searches": every phrase, the
    question itself first (on Wikipedia it usually finds the overview article)}."""
    p = outline.plan(question, list(heads), _budget(mode), MODEL)
    phrases = [clean_phrase(question)] + [s for sec in p["sections"] for s in sec["searches"]]
    phrases = list(dict.fromkeys(x for x in phrases if x))
    return dict(p, searches=phrases[:MAX_SEARCHES] if mode != "deep" else phrases)


class _Reader:
    """Online Wikipedia, or the offline copy: switches by itself if the internet drops."""

    def __init__(self):
        self.offline = not spider.online() and spider.offline_available()
        self.failed: list[str] = []
        self.network_errors = 0
        self.searches = 0
        self.empty: list[str] = []

    def search(self, phrases: list[str], limit: int, skip: set[str]) -> list[dict]:
        if not phrases or STOP.is_set():
            return []
        self.searches += len(phrases)
        if not self.offline:
            got = spider.wikipedia(phrases, limit, should_stop=STOP.is_set, skip=skip)
            self.failed += got["failed"]
            self.empty += got.get("empty", [])
            if got["network_errors"] == len(phrases) and not got["articles"]:
                self.network_errors += len(phrases)
                if spider.offline_available():
                    self.offline = True               # the internet dropped: read the offline copy
                else:
                    return []
            else:
                return got["articles"]
        got = spider.offline_wikipedia(phrases, limit + len(skip), should_stop=STOP.is_set)
        self.failed += got.get("failed", [])
        out = []
        for a in got["articles"]:
            if a["title"] in skip:
                continue
            a["text"] = elephant.html_to_text(a.pop("html"))
            out.append(a)
        return out[:limit]


def _overview(question: str, reader: _Reader) -> tuple[dict | None, list[str]]:
    """The article the question itself finds first, and its section headings (read by code)."""
    arts = reader.search([clean_phrase(question)], 1, set())
    if not arts:
        return None, []
    return arts[0], outline.headings(arts[0].get("text", ""))


def make_plan(j: dict) -> dict:
    """A deep job's plan, for your OK. Uses the chat model once, and one Wikipedia search."""
    reader = _Reader()
    _, heads = _overview(j["question"], reader) if (spider.online() or spider.offline_available()) else (None, [])
    p = plan(j["question"], heads, "deep")
    set_plan(j["id"], p["sections"], p["needs_news"], by="lobster")
    return p


def plan_text(job_id: int, question: str, sections: list[dict]) -> str:
    lines = "\n".join(f"{i}. {s['question']}" for i, s in enumerate(sections, 1))
    return (f"🐘 Plan for deep research #{job_id}: {question}\n\n{lines}\n\n"
            f"Once you OK it, it runs tonight ({window_text()}) and the brief is here in the morning. "
            f"To change it: /plan {job_id}, then one sub-question per line.")


# --- 2-4. Collect, judge, hand to the Elephant ------------------------------------------------

def _judge(question: str, section_qs: list[str], cands: list[tuple[dict, int]], per_section: int,
           cap: int) -> tuple[list[tuple[dict, int]], list[dict]]:
    """Skip articles off the question (and off their sub-question); then keep each
    sub-question's best few, taking turns so every sub-question gets its share."""
    if not cands:
        return [], []
    texts = [a["text"][:600] for a, _ in cands]
    m = elephant.embed([question] + section_qs + texts)
    nq = 1 + len(section_qs)
    if m is not None and m.shape[0] == nq + len(texts):
        sims = m[nq:] @ m[:nq].T                                    # (articles, 1 + sections)
    else:
        import numpy as np
        sims = np.stack([elephant._overlap(q, texts) for q in [question] + section_qs], axis=1)
    fit = [float(max(sims[k, 0], sims[k, i + 1] if i >= 0 else 0.0)) for k, (_, i) in enumerate(cands)]
    best = max(fit)
    on = [k for k in range(len(cands)) if fit[k] >= best * elephant.RELEVANCE_FLOOR and fit[k] > 0]
    skipped = [cands[k][0] for k in range(len(cands)) if k not in on]
    queues: dict[int, list[int]] = {}
    for k in sorted(on, key=lambda k: -(sims[k, cands[k][1] + 1] if cands[k][1] >= 0 else sims[k, 0])):
        queues.setdefault(cands[k][1], []).append(k)
    kept: list[int] = []
    turn = 0
    while len(kept) < cap and any(queues.values()):
        for i in sorted(queues):
            if queues[i] and len(kept) < cap and (i < 0 or turn < per_section):
                kept.append(queues[i].pop(0))
        turn += 1
        if turn > per_section + 1:
            break
    skipped += [cands[k][0] for k in on if k not in kept]
    return [cands[k] for k in sorted(kept)], skipped


def _collect(j: dict, todo: dict[int, list[str]], sections: list[dict], reader: _Reader, seen: dict,
             budget: dict, overview: dict | None = None) -> tuple[list[str], list[dict], dict]:
    """One round: search each sub-question's phrases, judge, and hand the kept articles to the
    Elephant. Returns (new urls, skipped articles, {url: offline copy name})."""
    q = j["question"]
    section_qs = [s["question"] for s in sections]
    cands: list[tuple[dict, int]] = [(overview, -1)] if overview else []
    titles = set(seen["titles"]) | ({overview["title"]} if overview else set())
    for i, phrases in todo.items():
        if STOP.is_set():
            break
        for a in reader.search(phrases, budget["per_section"] + 1, titles):
            if a["title"] not in titles and a["url"] not in seen["urls"]:
                titles.add(a["title"])
                cands.append((a, i))
    kept, skipped = _judge(q, section_qs, cands, budget["per_section"], budget["articles"])
    urls, copies = [], {}
    for a, _ in kept:
        spider.to_inbox({"url": a["url"], "title": a["title"], "fetched_at": a["fetched_at"],
                         "content_type": "text/plain", "content": f"{a['title']}. {a['text']}",
                         "sha256": a["sha256"], "aspects": section_qs,
                         "per_page": 14 if budget is outline.DEEP else 10}, origin="research", job=j["id"], question=q)
        urls.append(a["url"])
        if a.get("copy"):
            copies[a["url"]] = a["copy"]
    seen["titles"] = sorted(titles | {a["title"] for a, _ in cands})
    seen["urls"] = list(dict.fromkeys(seen["urls"] + urls))
    return urls, skipped, copies


def _news(j: dict, reader: _Reader, sections: list[dict]) -> str | None:
    if reader.offline:
        return None
    try:
        page = spider.news(clean_phrase(j["question"]))
        spider.to_inbox(dict(page, aspects=[s["question"] for s in sections]), origin="research",
                        job=j["id"], question=j["question"])
        return page["url"]
    except Exception as e:
        reader.failed.append(f"news: {type(e).__name__}")
        return None


# --- The whole job ----------------------------------------------------------------

def run_job(j: dict, say=lambda text: None, progress=None, should_yield=None) -> dict:
    """Run one job. say(text) reports milestones; progress(i, n, url) reports pages; a deep job
    checks should_yield() between rounds and steps aside (keeping its place) for quick jobs.
    Returns {"status": done | nothing | offline | stopped | paused | yield, ...}."""
    if shiba.paused():
        return {"status": "paused"}
    if j.get("mode") == "deep":
        return _run_deep(j, say, progress, should_yield)
    return _run_quick(j, say, progress)


def _finish(j: dict, urls: list[str], b: dict, started: float, extra: dict) -> None:
    from config import VERSION
    metrics = dict(b.get("metrics", {}), seconds=round(time.monotonic() - started), version=VERSION, **extra)
    mark_done(j["id"], "done", len(urls), brief=b["text"], topic=b["topic"], cards=b["cards"], metrics=metrics)


def _run_quick(j: dict, say, progress) -> dict:
    q = j["question"]
    started = time.monotonic()
    with timed("research"):
        reader = _Reader()
        with timed("plan"):
            overview, heads = _overview(q, reader)
            p = plan(q, heads)
        if STOP.is_set():
            return {"status": "stopped"}
        sections = p["sections"]
        say("🕷️ Out collecting: " + " · ".join(s["question"] for s in sections))
        seen = {"titles": [], "urls": []}
        with timed("collect"):
            todo = {i: s["searches"] for i, s in enumerate(sections)}
            urls, skipped, copies = _collect(j, todo, sections, reader, seen, outline.QUICK, overview)
            if p["needs_news"] and len(urls) < outline.QUICK["articles"]:
                news_url = _news(j, reader, sections)
                if news_url:
                    urls.append(news_url)
        shiba.audit("research_gathered", job=j["id"], pages=len(urls), skipped=len(skipped),
                    failed=len(reader.failed), empty=reader.empty[:10])
        if STOP.is_set():
            return {"status": "stopped"}
        if not urls:
            if reader.network_errors and not reader.offline and reader.network_errors >= reader.searches:
                return {"status": "offline"}          # stays queued until the tablet is back online
            mark_done(j["id"], "nothing found")
            return {"status": "nothing", "failed": reader.failed + [f"nothing found for '{e}'" for e in reader.empty[:3]]}
        skipped_note = f" Skipped as off-topic: {', '.join(a['title'] for a in skipped)}." if skipped else ""
        where = " from your offline Wikipedia" if reader.offline else ""
        say(f"🕷️ Back with {len(urls)} article{'s' if len(urls) != 1 else ''}{where}.{skipped_note} Handing them to the Elephant.")
        r = elephant.digest(job=j["id"], progress=progress, should_stop=STOP.is_set, unload=False)
        if r["stopped"] or STOP.is_set():
            return {"status": "stopped", "facts": r["facts"]}
        b = elephant.outline_brief(j, sections, urls, copies, outline.QUICK, WRITER_MODEL)
    _finish(j, urls, b, started, {"rounds": 1})
    return {"status": "done", "brief": b["text"], "facts": r["facts"], "rejected": r["rejected"],
            "suspicious": r["suspicious"], "metrics": b["metrics"]}


# --- Deep jobs: rounds that fill the gaps, with a saved place ----------------------------------

def _state_path(job_id: int):
    return DEEP_DIR / f"{job_id}.json"


def _load_state(job_id: int) -> dict | None:
    try:
        return json.loads(_state_path(job_id).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None


def _save_state(job_id: int, st: dict) -> None:
    DEEP_DIR.mkdir(parents=True, exist_ok=True)
    write_atomic(_state_path(job_id), json.dumps(st, ensure_ascii=False))


def deep_writer() -> tuple[str, str]:
    """(writer model, note for the brief). The bigger writer if it's downloaded."""
    if DEEP_WRITER_MODEL and DEEP_WRITER_MODEL != WRITER_MODEL and not ollama.installed(DEEP_WRITER_MODEL):
        return WRITER_MODEL, (f"(Written with {WRITER_MODEL}: the bigger writer isn't downloaded. "
                              f"To use it: ollama pull {DEEP_WRITER_MODEL})")
    return DEEP_WRITER_MODEL or WRITER_MODEL, ""


def _run_deep(j: dict, say, progress, should_yield) -> dict:
    if not j.get("plan"):
        return {"status": "nothing", "failed": ["no plan yet"]}
    sections = j["plan"]["sections"]
    questions = [s["question"] for s in sections]
    budget = outline.DEEP
    st = _load_state(j["id"]) or {"round": 0, "seconds": 0.0, "titles": [], "urls": [], "copies": {},
                                  "tried": {str(i): list(s["searches"]) for i, s in enumerate(sections)},
                                  "skipped": 0, "log": []}
    if st["round"] == 0:
        say(f"🕷️ Deep research #{j['id']} starting: {len(sections)} sub-questions, up to {budget['rounds']} rounds.")
    else:
        say(f"🕷️ Deep research #{j['id']} picking up after round {st['round']}.")
    reader = _Reader()
    with timed("research_deep"):
        while st["round"] < budget["rounds"]:
            if STOP.is_set():
                return {"status": "stopped"}
            if st["seconds"] >= DEEP_MAX_MINUTES * 60:
                st["log"].append("time cap reached")
                break
            if should_yield is not None and should_yield():
                _save_state(j["id"], st)
                return {"status": "yield"}
            t0 = time.monotonic()
            n = st["round"] + 1
            if n == 1:
                todo = {i: s["searches"] for i, s in enumerate(sections)}
                say(f"🕷️ Round 1 of {budget['rounds']}: " + " · ".join(questions))
            else:
                _, _, cover = elephant.organize(questions, st["urls"], budget["candidates"], need_sources=2)
                thin = [i for i, c in enumerate(cover) if c["thin"]]
                if not thin:
                    st["log"].append(f"round {n}: every section covered")
                    break
                todo = {}
                for i in thin:
                    every = [x for ps in st["tried"].values() for x in ps]     # never repeat a search in this job
                    fresh = outline.gap_searches(questions[i], every, MODEL)
                    if fresh:
                        todo[i] = fresh
                        st["tried"].setdefault(str(i), []).extend(fresh)
                if not todo:
                    st["log"].append(f"round {n}: no new searches for the thin sections")
                    break
                say(f"🕷️ Round {n} of {budget['rounds']} · filling: " + "; ".join(questions[i] for i in todo))
            overview = _overview(j["question"], reader)[0] if n == 1 else None
            urls, skipped, copies = _collect(j, todo, sections, reader, st, budget, overview)
            if n == 1 and j["plan"].get("needs_news"):
                news_url = _news(j, reader, sections)
                if news_url:
                    urls.append(news_url)
                    st["urls"].append(news_url)
            st["copies"].update(copies)
            st["skipped"] += len(skipped)
            if urls:
                r = elephant.digest(job=j["id"], progress=progress, should_stop=STOP.is_set, unload=False)
                if r["stopped"] or STOP.is_set():
                    return {"status": "stopped", "facts": r["facts"]}
            elif n == 1 and reader.network_errors and not reader.offline:
                return {"status": "offline"}
            st["round"] = n
            st["seconds"] += time.monotonic() - t0
            st["log"].append(f"round {n}: {len(urls)} new articles")
            _save_state(j["id"], st)
        shiba.audit("research_gathered", job=j["id"], pages=len(st["urls"]), rounds=st["round"],
                    failed=len(reader.failed), empty=reader.empty[:10], mode="deep")
        if not st["urls"]:
            mark_done(j["id"], "nothing found")
            return {"status": "nothing", "failed": reader.failed}
        writer, note = deep_writer()
        say(f"🐘 Writing the brief from {len(st['urls'])} articles" + (f" with {writer}" if writer != WRITER_MODEL else "") + ".")
        if writer != MODEL:
            ollama.unload(MODEL)                     # room for the bigger writer; chat reloads it when needed
        try:
            b = elephant.outline_brief(j, sections, st["urls"], st["copies"], budget, writer, deep=True, note=note)
        finally:
            if writer != MODEL:
                ollama.unload(writer)
    _finish(j, st["urls"], b, time.monotonic() - st["seconds"], {"rounds": st["round"]})
    try:
        _state_path(j["id"]).rename(DEEP_DIR / f"{j['id']}.done.json")   # kept, not deleted
    except OSError:
        pass
    return {"status": "done", "brief": b["text"], "facts": None, "rejected": 0, "suspicious": [],
            "metrics": b["metrics"]}
