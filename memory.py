"""The Elephant's vault: facts + embeddings in SQLite, searched with numpy.

Every fact carries a `tag`:
  "owner" -> came from you (MEMORY.md or the remember action). Trusted.
  "web"   -> extracted from a web page by the Elephant. Untrusted data.

For a few thousand facts, comparing against every vector with numpy takes
milliseconds, so no vector-database server is needed.
"""
import hashlib
import sqlite3
import time

import numpy as np

import ollama
from config import DB_PATH, EMBED_MODEL, MEMORY_MD

SCHEMA = """
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
"""


def _db() -> sqlite3.Connection:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(DB_PATH)
    con.execute("PRAGMA journal_mode=WAL")  # safer if the tablet dies mid-write
    con.executescript(SCHEMA)
    return con


def _hash(text: str) -> str:
    return hashlib.sha256(" ".join(text.lower().split()).encode()).hexdigest()


def add_facts(texts: list[str], tag: str, source: str) -> int:
    """Embed and store new facts. Returns how many were actually new."""
    con = _db()
    known = {h for (h,) in con.execute("SELECT text_hash FROM facts WHERE tag=?", (tag,))}
    new = [t for t in dict.fromkeys(texts) if _hash(t) not in known]
    if not new:
        return 0
    vectors = ollama.embed(EMBED_MODEL, new)
    now = time.strftime("%Y-%m-%d %H:%M")
    with con:
        con.executemany(
            "INSERT OR IGNORE INTO facts (text, tag, source, added, text_hash, embedding) VALUES (?,?,?,?,?,?)",
            [(t, tag, source, now, _hash(t), np.asarray(v, dtype=np.float32).tobytes())
             for t, v in zip(new, vectors)],
        )
    con.close()
    return len(new)


def search(query: str, k: int = 6, min_score: float = 0.45) -> list[dict]:
    con = _db()
    rows = con.execute("SELECT text, tag, source, added, embedding FROM facts").fetchall()
    con.close()
    if not rows:
        return []
    q = np.asarray(ollama.embed(EMBED_MODEL, [query])[0], dtype=np.float32)
    mat = np.stack([np.frombuffer(r[4], dtype=np.float32) for r in rows])
    scores = mat @ q / (np.linalg.norm(mat, axis=1) * np.linalg.norm(q) + 1e-9)
    best = np.argsort(-scores)[:k]
    return [
        {"text": rows[i][0], "tag": rows[i][1], "source": rows[i][2],
         "added": rows[i][3], "score": round(float(scores[i]), 3)}
        for i in best if scores[i] >= min_score
    ]


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
    with con:
        for (h,) in con.execute("SELECT text_hash FROM facts WHERE tag='owner'").fetchall():
            if h not in wanted:
                con.execute("DELETE FROM facts WHERE text_hash=? AND tag='owner'", (h,))
    con.close()
    return add_facts(lines, "owner", "MEMORY.md")
