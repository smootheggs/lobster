"""The Elephant's vault: facts with receipts in SQLite.

Every fact carries a `tag`:
  "owner" -> came from you (MEMORY.md or the remember action). Trusted.
  "web"   -> extracted from a web page by the Elephant. Untrusted data.

Web facts also keep their receipt: the source URL, the exact sentence they
came from, when they were fetched and published, whether they are a plain
fact or a reported claim ("X said..."), and a review status.

Search uses embeddings (numpy, no vector server) when the embedding model is
available, and SQLite full-text search otherwise, so it never goes blind.

Schema changes only ever ADD columns or tables; nothing is dropped.
"""
import hashlib
import re
import sqlite3
import time
from pathlib import Path

import numpy as np

import ollama
from config import BACKUP_DIR, DB_PATH, EMBED_MODEL, MEMORY_MD

SCHEMA_VERSION = 2
KEEP_BACKUPS = 14

BASE = """
CREATE TABLE IF NOT EXISTS facts (
    id        INTEGER PRIMARY KEY,
    text      TEXT NOT NULL,
    tag       TEXT NOT NULL CHECK (tag IN ('owner', 'web')),
    source    TEXT NOT NULL,          -- URL, or 'MEMORY.md'
    added     TEXT NOT NULL,
    text_hash TEXT NOT NULL,
    embedding BLOB NOT NULL,
    UNIQUE (text_hash, tag)           -- stops the same fact being stored twice
);
CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL);
"""
V2_COLUMNS = {                         # name -> definition, added in version 2
    "sentence": "TEXT NOT NULL DEFAULT ''",
    "fetched_at": "TEXT NOT NULL DEFAULT ''",
    "published_at": "TEXT NOT NULL DEFAULT ''",
    "kind": "TEXT NOT NULL DEFAULT 'fact'",          # fact | reported_claim
    "status": "TEXT NOT NULL DEFAULT 'unreviewed'",  # unreviewed | verified | conflict | stale
    "job": "INTEGER",
}
REPORTED = re.compile(r"\b(said|says|according to|claimed|claims|reported|announced|"
                      r"expects?|plans? to|alleged|believes?)\b", re.I)

_ready: set[str] = set()


def _has_fts(con) -> bool:
    return con.execute("SELECT 1 FROM sqlite_master WHERE name='facts_fts'").fetchone() is not None


def _migrate(con: sqlite3.Connection) -> None:
    con.executescript(BASE)
    have = {row[1] for row in con.execute("PRAGMA table_info(facts)")}
    with con:
        for name, definition in V2_COLUMNS.items():
            if name not in have:
                con.execute(f"ALTER TABLE facts ADD COLUMN {name} {definition}")
        try:                                     # full-text search, if this SQLite has FTS5
            if not _has_fts(con):
                con.execute("CREATE VIRTUAL TABLE facts_fts USING fts5(text)")
                con.execute("INSERT INTO facts_fts(rowid, text) SELECT id, text FROM facts")
        except sqlite3.OperationalError:
            pass
        con.execute("DELETE FROM schema_version")
        con.execute("INSERT INTO schema_version VALUES (?)", (SCHEMA_VERSION,))


def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")  # safer if the tablet dies mid-write
    if str(DB_PATH) not in _ready:
        _migrate(con)
        _ready.add(str(DB_PATH))
    return con


def _hash(text: str) -> str:
    return hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()


def kind_of(text: str) -> str:
    """'reported_claim' when the fact is someone's statement rather than an observed fact."""
    return "reported_claim" if REPORTED.search(text) else "fact"


def add_facts(items: list, tag: str, source: str, page: dict | None = None) -> int:
    """Embed and store new facts. Returns how many were actually new.

    items: plain strings, or dicts {"text": ..., "sentence": ...} carrying the receipt.
    page:  optional metadata from the Spider (fetched_at, published_at, job).
    """
    page = page or {}
    rows = [i if isinstance(i, dict) else {"text": i, "sentence": ""} for i in items]
    con = _db()
    known = {h for (h,) in con.execute("SELECT text_hash FROM facts WHERE tag=?", (tag,))}
    new, seen = [], set()
    for r in rows:
        h = _hash(r["text"])
        if h not in known and h not in seen:
            seen.add(h)
            new.append(r)
    if not new:
        con.close()
        return 0
    try:
        vectors = ollama.embed(EMBED_MODEL, [r["text"] for r in new])
    except Exception:                    # embedding model missing: keyword search still works
        vectors = [[] for _ in new]
    now = time.strftime("%Y-%m-%d %H:%M")
    fts = _has_fts(con)
    with con:
        for r, v in zip(new, vectors):
            cur = con.execute(
                "INSERT OR IGNORE INTO facts (text, tag, source, added, text_hash, embedding, sentence, "
                "fetched_at, published_at, kind, job) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                (r["text"], tag, source, now, _hash(r["text"]), np.asarray(v, dtype=np.float32).tobytes(),
                 r.get("sentence", ""), page.get("fetched_at", ""), page.get("published_at", ""),
                 kind_of(r["text"]) if tag == "web" else "fact", page.get("job")))
            if fts and cur.rowcount:
                con.execute("INSERT INTO facts_fts(rowid, text) VALUES (?, ?)", (cur.lastrowid, r["text"]))
    con.close()
    return len(new)


def _row(r, score) -> dict:
    return {"text": r[0], "tag": r[1], "source": r[2], "added": r[3], "score": round(float(score), 3)}


def keyword_search(query: str, k: int = 6) -> list[dict]:
    """Full-text search; works without any model."""
    words = re.findall(r"\w{3,}", query.lower())
    if not words:
        return []
    con = _db()
    try:
        if not _has_fts(con):
            return []
        rows = con.execute(
            "SELECT f.text, f.tag, f.source, f.added FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid "
            "WHERE facts_fts MATCH ? ORDER BY rank LIMIT ?", (" OR ".join(words), k)).fetchall()
    finally:
        con.close()
    return [_row(r, 1.0) for r in rows]


def search(query: str, k: int = 6, min_score: float = 0.45) -> list[dict]:
    """Meaning-based search with embeddings; falls back to keywords if the model is unavailable."""
    con = _db()
    rows = con.execute("SELECT text, tag, source, added, embedding FROM facts WHERE length(embedding) > 0").fetchall()
    con.close()
    if not rows:
        return keyword_search(query, k)
    try:
        q = np.asarray(ollama.embed(EMBED_MODEL, [query])[0], dtype=np.float32)
    except Exception:
        return keyword_search(query, k)
    mat = np.stack([np.frombuffer(r[4], dtype=np.float32) for r in rows])
    if mat.shape[1] != q.shape[0]:            # embedding model changed: don't mix vectors
        return keyword_search(query, k)
    scores = mat @ q / (np.linalg.norm(mat, axis=1) * np.linalg.norm(q) + 1e-9)
    best = np.argsort(-scores)[:k]
    return [_row(rows[i], scores[i]) for i in best if scores[i] >= min_score]


def facts_for_sources(urls: list[str], limit: int = 40, per_source: int = 8) -> list[dict]:
    """Web facts from these pages for a research brief.

    Takes up to per_source facts from each page, earliest first (a Wikipedia
    article's opening summary comes first), so no single page crowds out the rest.
    """
    if not urls:
        return []
    con = _db()
    out = []
    for url in dict.fromkeys(urls):
        rows = con.execute("SELECT text, source FROM facts WHERE tag='web' AND source=? ORDER BY id LIMIT ?",
                           (url, per_source)).fetchall()
        out.extend({"text": t, "source": s} for t, s in rows)
    con.close()
    return out[:limit]


def stats() -> dict:
    con = _db()
    counts = dict(con.execute("SELECT tag, COUNT(*) FROM facts GROUP BY tag").fetchall())
    con.close()
    return {"owner": counts.get("owner", 0), "web": counts.get("web", 0)}


# --- Backups -----------------------------------------------------------------------

def backup() -> dict:
    """Copy the live database with SQLite's backup API, check the copy, keep the newest 14.

    Older backup COPIES beyond 14 are removed (the live database is never touched),
    so backups can't fill the tablet. Point BACKUP_DIR into your vault to sync them to Drive.
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    dest = BACKUP_DIR / f"memory-{time.strftime('%Y%m%d-%H%M%S')}.db"
    src = _db()
    out = sqlite3.connect(dest)
    try:
        src.backup(out)
        live = src.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
        copied = out.execute("SELECT COUNT(*) FROM facts").fetchone()[0]
    finally:
        out.close()
        src.close()
    if copied != live:
        raise RuntimeError(f"backup check failed: {copied} facts copied, {live} in the live database")
    backups = sorted(BACKUP_DIR.glob("memory-*.db"))
    for old in backups[:-KEEP_BACKUPS]:
        old.unlink()
    return {"file": dest.name, "facts": copied, "kept": min(len(backups), KEEP_BACKUPS)}


def last_backup() -> str | None:
    backups = sorted(BACKUP_DIR.glob("memory-*.db")) if BACKUP_DIR.exists() else []
    if not backups:
        return None
    return time.strftime("%Y-%m-%d %H:%M", time.localtime(backups[-1].stat().st_mtime))


# --- MEMORY.md: your own, human-editable list of trusted facts ---------------

def remember(text: str) -> None:
    """Append a fact to MEMORY.md (the source of truth) and index it."""
    MEMORY_MD.parent.mkdir(parents=True, exist_ok=True)
    if not MEMORY_MD.exists():
        MEMORY_MD.write_text("# Things the Lobster knows about me\n\n", encoding="utf-8")
    with MEMORY_MD.open("a", encoding="utf-8") as f:
        f.write(f"- {text.strip()}\n")
    add_facts([text.strip()], "owner", "MEMORY.md")


def sync_memory_md() -> int:
    """Re-index MEMORY.md after you edit it by hand.

    MEMORY.md is the source of truth for owner facts, so lines you removed
    from the file are dropped from the search index too. The file itself is
    never touched here.
    """
    if not MEMORY_MD.exists():
        return 0
    lines = [l[2:].strip() for l in MEMORY_MD.read_text(encoding="utf-8").splitlines()
             if l.startswith("- ") and l[2:].strip()]
    wanted = {_hash(l) for l in lines}
    con = _db()
    fts = _has_fts(con)
    with con:
        for fid, h in con.execute("SELECT id, text_hash FROM facts WHERE tag='owner'").fetchall():
            if h not in wanted:
                con.execute("DELETE FROM facts WHERE id=?", (fid,))
                if fts:
                    con.execute("DELETE FROM facts_fts WHERE rowid=?", (fid,))
    con.close()
    return add_facts(lines, "owner", "MEMORY.md")
