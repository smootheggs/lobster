"""🐘 The Elephant's index: facts with receipts, and the cards made from them, in SQLite.

Every fact carries a `tag`:
  "owner" -> came from you (a note, MEMORY.md). Trusted.
  "web"   -> collected from a web page. Untrusted data.

Web facts are the source's own sentences (never a model's rewording), with the
surrounding sentences as their receipt, plus when they were fetched, which
research job found them, and a review `status`:
  unreviewed -> new · verified -> you checked it · wrong -> you marked it wrong (never used again, never deleted)

The `cards` table remembers every note the Elephant wrote into your vault, so
your reviews in Obsidian or the app flow back here.

Search ranks by meaning (embeddings, held in memory and loaded incrementally)
and falls back to SQLite full-text search when the embedding model is away.
Schema changes only ever ADD columns, tables or indexes; nothing is dropped.
"""
import hashlib
import re
import sqlite3
import threading
import time

import numpy as np

import ollama
from config import BACKUP_DIR, DB_PATH, EMBED_MODEL, MEMORY_MD

SCHEMA_VERSION = 4
KEEP_BACKUPS = 14
STATUSES = ("unreviewed", "verified", "wrong", "set_aside")   # set_aside: kept and used, just not worth reviewing

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
    "sentence": "TEXT NOT NULL DEFAULT ''",          # the receipt: the sentence and its neighbours
    "fetched_at": "TEXT NOT NULL DEFAULT ''",
    "published_at": "TEXT NOT NULL DEFAULT ''",
    "kind": "TEXT NOT NULL DEFAULT 'fact'",          # fact | reported_claim
    "status": "TEXT NOT NULL DEFAULT 'unreviewed'",  # unreviewed | verified | wrong
    "job": "INTEGER",
    "section": "TEXT NOT NULL DEFAULT ''",          # v4: the heading of the part of the page it came from
}
V3 = """
CREATE TABLE IF NOT EXISTS cards (
    id       TEXT PRIMARY KEY,        -- stable: survives renaming or moving the file
    kind     TEXT NOT NULL,           -- card | note | source | map
    fact_id  INTEGER,
    title    TEXT NOT NULL,
    path     TEXT NOT NULL,           -- relative to the vault
    job      INTEGER,
    created  TEXT NOT NULL,
    status   TEXT NOT NULL DEFAULT 'unreviewed'
);
CREATE INDEX IF NOT EXISTS facts_source ON facts(source);
CREATE INDEX IF NOT EXISTS facts_job ON facts(job);
CREATE INDEX IF NOT EXISTS cards_kind ON cards(kind, created);
"""
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
        con.executescript(V3)
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
    con = sqlite3.connect(DB_PATH, timeout=30)
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
    """Store new facts. Returns how many were actually new.

    items: plain strings, or dicts {"text", "sentence", optional "embedding"} (the Elephant
           passes the embedding it already computed, so nothing is embedded twice).
    page:  optional metadata (fetched_at, published_at, job).
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
    missing = [r for r in new if r.get("embedding") is None]
    if missing:
        try:
            for r, v in zip(missing, ollama.embed(EMBED_MODEL, [r["text"] for r in missing])):
                r["embedding"] = v
        except Exception:                # embedding model away: keyword search still works
            pass
    now = time.strftime("%Y-%m-%d %H:%M")
    fts = _has_fts(con)
    with con:
        for r in new:
            cur = con.execute(
                "INSERT OR IGNORE INTO facts (text, tag, source, added, text_hash, embedding, sentence, "
                "fetched_at, published_at, kind, job, section) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (r["text"], tag, source, now, _hash(r["text"]),
                 np.asarray(r.get("embedding") or [], dtype=np.float32).tobytes(),
                 r.get("sentence", ""), page.get("fetched_at", ""), page.get("published_at", ""),
                 kind_of(r["text"]) if tag == "web" else "fact", page.get("job"), r.get("section", "")[:120]))
            if fts and cur.rowcount:
                con.execute("INSERT INTO facts_fts(rowid, text) VALUES (?, ?)", (cur.lastrowid, r["text"]))
    con.close()
    return len(new)


# --- Search ---------------------------------------------------------------------

class _Vectors:
    """All fact embeddings, normalised, held in memory (half precision) and topped up
    with only the new rows on each search. Fast up to tens of thousands of facts."""

    def __init__(self):
        self.lock = threading.Lock()
        self.reset()

    def reset(self):
        self.db, self.max_id, self.dim = "", 0, 0
        self.ids = np.zeros(0, dtype=np.int64)
        self.mat = np.zeros((0, 0), dtype=np.float16)

    def load(self, con) -> None:
        if self.db != str(DB_PATH):
            self.reset()
            self.db = str(DB_PATH)
        rows = con.execute("SELECT id, embedding FROM facts WHERE id > ? AND length(embedding) > 0 ORDER BY id",
                           (self.max_id,)).fetchall()
        if not rows:
            return
        vecs, ids = [], []
        for fid, blob in rows:
            v = np.frombuffer(blob, dtype=np.float32)
            if self.dim == 0:
                self.dim = len(v)
            if len(v) == self.dim:            # embedding model changed: don't mix vectors
                n = np.linalg.norm(v)
                vecs.append(v / n if n else v)
                ids.append(fid)
        self.max_id = rows[-1][0]
        if vecs:
            new = np.stack(vecs).astype(np.float16)
            self.mat = new if self.mat.size == 0 else np.vstack([self.mat, new])
            self.ids = np.concatenate([self.ids, np.asarray(ids, dtype=np.int64)])


_vectors = _Vectors()


def _row(r, score) -> dict:
    return {"id": r[0], "text": r[1], "tag": r[2], "source": r[3], "added": r[4], "status": r[5],
            "sentence": r[6], "score": round(float(score), 3)}


_COLS = "f.id, f.text, f.tag, f.source, f.added, f.status, f.sentence"


def keyword_search(query: str, k: int = 6) -> list[dict]:
    """Full-text search; works without any model. Facts marked wrong are skipped."""
    words = re.findall(r"\w{3,}", query.lower())
    if not words:
        return []
    con = _db()
    try:
        if not _has_fts(con):
            return []
        rows = con.execute(
            f"SELECT {_COLS} FROM facts_fts JOIN facts f ON f.id = facts_fts.rowid "
            "WHERE facts_fts MATCH ? AND f.status != 'wrong' ORDER BY rank LIMIT ?",
            (" OR ".join(f'"{w}"' for w in words), k)).fetchall()
    finally:
        con.close()
    return [_row(r, 1.0) for r in rows]


def search(query: str, k: int = 6, min_score: float = 0.45) -> list[dict]:
    """Meaning-based search; falls back to keywords if the embedding model is unavailable.
    Verified facts get a small boost; facts you marked wrong never come back."""
    con = _db()
    try:
        with _vectors.lock:
            _vectors.load(con)
            ids, mat, dim = _vectors.ids, _vectors.mat, _vectors.dim
        if not len(ids):
            return keyword_search(query, k)
        try:
            q = np.asarray(ollama.embed(EMBED_MODEL, [query])[0], dtype=np.float32)
        except Exception:
            return keyword_search(query, k)
        if q.shape[0] != dim:
            return keyword_search(query, k)
        q = q / (np.linalg.norm(q) + 1e-9)
        scores = mat.astype(np.float32) @ q
        top = np.argsort(-scores)[: k * 4]
        wanted = {int(ids[i]): float(scores[i]) for i in top if scores[i] >= min_score}
        if not wanted:
            return []
        marks = ",".join("?" * len(wanted))
        rows = con.execute(f"SELECT {_COLS} FROM facts f WHERE f.id IN ({marks}) AND f.status != 'wrong'",
                           list(wanted)).fetchall()
    finally:
        con.close()
    hits = [_row(r, wanted[r[0]] + (0.05 if r[5] == "verified" else 0.0)) for r in rows]
    return sorted(hits, key=lambda h: -h["score"])[:k]


def similar_facts(fact_id: int, k: int = 12) -> list[dict]:
    """Other web facts closest in meaning to this one (its own stored vector, so no model call).
    Used to check a card against what other sources say."""
    con = _db()
    try:
        with _vectors.lock:
            _vectors.load(con)
            ids, mat = _vectors.ids, _vectors.mat
        where = np.nonzero(ids == fact_id)[0]
        if not len(where):
            return []
        v = mat[where[0]].astype(np.float32)
        scores = mat.astype(np.float32) @ v
        top = [int(i) for i in np.argsort(-scores)[: k + 1] if int(ids[i]) != fact_id][:k]
        wanted = {int(ids[i]): float(scores[i]) for i in top}
        if not wanted:
            return []
        marks = ",".join("?" * len(wanted))
        rows = con.execute(f"SELECT {_COLS} FROM facts f WHERE f.id IN ({marks}) AND f.tag='web' "
                           "AND f.status != 'wrong'", list(wanted)).fetchall()
    finally:
        con.close()
    return sorted((_row(r, wanted[r[0]]) for r in rows), key=lambda h: -h["score"])


def card_detail(card_id: str) -> dict | None:
    """A card with its fact, the source sentence and its neighbours, and where it came from."""
    con = _db()
    r = con.execute("SELECT c.id, c.title, c.path, c.job, c.status, c.fact_id, f.text, f.sentence, f.source, "
                    "f.fetched_at, f.section FROM cards c LEFT JOIN facts f ON f.id = c.fact_id "
                    "WHERE c.id=? AND c.kind='card'", (card_id,)).fetchone()
    con.close()
    keys = ("id", "title", "path", "job", "status", "fact_id", "text", "context", "source", "fetched_at", "section")
    return dict(zip(keys, r)) if r else None


def facts_for_sources(urls: list[str], limit: int = 40, per_source: int = 8) -> list[dict]:
    """Web facts from these pages for a research brief: up to per_source from each page,
    in page order (an article's opening comes first), so no single page crowds out the rest."""
    if not urls:
        return []
    con = _db()
    out = []
    for url in dict.fromkeys(urls):
        rows = con.execute("SELECT id, text, source, sentence, section FROM facts WHERE tag='web' AND source=? "
                           "AND status != 'wrong' ORDER BY id LIMIT ?", (url, per_source)).fetchall()
        out.extend({"id": i, "text": t, "source": s, "sentence": w, "section": h} for i, t, s, w, h in rows)
    con.close()
    return out[:limit]


def stats() -> dict:
    con = _db()
    counts = dict(con.execute("SELECT tag, COUNT(*) FROM facts GROUP BY tag").fetchall())
    cards = dict(con.execute("SELECT status, COUNT(*) FROM cards WHERE kind='card' GROUP BY status").fetchall())
    con.close()
    return {"owner": counts.get("owner", 0), "web": counts.get("web", 0),
            "cards": sum(cards.values()), "unreviewed": cards.get("unreviewed", 0)}


# --- Cards: what the Elephant wrote into your vault ---------------------------------

def add_card(card_id: str, kind: str, title: str, path: str, fact_id: int | None = None,
             job: int | None = None) -> None:
    con = _db()
    with con:
        con.execute("INSERT OR IGNORE INTO cards (id, kind, fact_id, title, path, job, created) "
                    "VALUES (?,?,?,?,?,?,?)", (card_id, kind, fact_id, title, path, job, time.strftime("%Y-%m-%d %H:%M")))
    con.close()


def cards(kind: str = "card", status: str | None = None, job: int | None = None, limit: int = 50) -> list[dict]:
    sql = ("SELECT c.id, c.kind, c.title, c.path, c.job, c.created, c.status, f.text, f.source "
           "FROM cards c LEFT JOIN facts f ON f.id = c.fact_id WHERE c.kind = ?")
    args: list = [kind]
    if status:
        sql += " AND c.status = ?"
        args.append(status)
    if job is not None:
        sql += " AND c.job = ?"
        args.append(job)
    con = _db()
    rows = con.execute(sql + " ORDER BY c.rowid DESC LIMIT ?", args + [limit]).fetchall()
    con.close()
    keys = ("id", "kind", "title", "path", "job", "created", "status", "text", "source")
    return [dict(zip(keys, r)) for r in rows]


def card(card_id: str) -> dict | None:
    con = _db()
    r = con.execute("SELECT id, kind, title, path, fact_id, status FROM cards WHERE id=?", (card_id,)).fetchone()
    con.close()
    return dict(zip(("id", "kind", "title", "path", "fact_id", "status"), r)) if r else None


def set_status(card_id: str, status: str, path: str | None = None) -> bool:
    """Record a review. The card's fact follows it: wrong facts are never used again."""
    if status not in STATUSES:
        return False
    con = _db()
    with con:
        cur = con.execute("UPDATE cards SET status=?" + (", path=?" if path else "") + " WHERE id=?",
                          (status, path, card_id) if path else (status, card_id))
        con.execute("UPDATE facts SET status=? WHERE id=(SELECT fact_id FROM cards WHERE id=?)", (status, card_id))
    con.close()
    return cur.rowcount > 0


# --- Backups -----------------------------------------------------------------------

def backup() -> dict:
    """Copy the live database with SQLite's backup API, check the copy, keep the newest 14.

    Older backup COPIES beyond 14 are removed (the live database is never touched),
    so backups can't fill the tablet. BACKUP_DIR sits in your Drive-synced work folder.
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
        MEMORY_MD.write_text("# Things Sloth Lobster knows about me\n\n", encoding="utf-8")
    with MEMORY_MD.open("a", encoding="utf-8") as f:
        f.write(f"- {' '.join(text.split())}\n")
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
    removed = False
    with con:
        for fid, h in con.execute("SELECT id, text_hash FROM facts WHERE tag='owner'").fetchall():
            if h not in wanted:
                removed = True
                con.execute("DELETE FROM facts WHERE id=?", (fid,))
                if fts:
                    con.execute("DELETE FROM facts_fts WHERE rowid=?", (fid,))
    con.close()
    if removed:
        with _vectors.lock:
            _vectors.reset()                   # rebuild the in-memory index without them
    return add_facts(lines, "owner", "MEMORY.md")
