"""🐘 The Elephant's pen: writes cards, source pages, maps and notes into your vault.

Card guide, version 1 (room to grow: every note carries card_version):

    ---
    id: lb-f42                 stable, survives renaming or moving the file
    type: card                 card | note | source | map
    loci: [world history]      lists from day one, even with one value
    status: unreviewed         unreviewed | verified | wrong (you change it)
    made_by: lobster           lobster | you
    sources: ["[[Hanseatic League (Wikipedia)]]"]
    maps: ["[[Hanseatic League]]"]
    created: 2026-10-22
    card_version: 1
    ---
    # Claim as a full phrase (the title is the file name)
    The claim, as the source states it.
    > The source sentence with its neighbours
    > — [[Source page]], fetched 2026-10-22

Rules this file enforces:
  - writes only inside VAULT_DIR/VAULT_INBOX; never outside, never into your own notes
  - never overwrites: a name that's taken gets " (2)", " (3)"...
  - atomic: each file is written to a hidden temp file, then renamed, so sync never
    uploads half a note
  - after writing, only the `status:` line of a Sloth Lobster note is ever changed, and only
    when you tap "Mark verified" or "Mark wrong"; your edits always win
  - reading back is cheap: only properties, only files changed since the last pass
"""
import json
import os
import re
import secrets
import time
from pathlib import Path

from config import DATA_DIR, VAULT_DIR, VAULT_INBOX
from store import write_atomic

CARD_VERSION = 1
INBOX = VAULT_DIR / VAULT_INBOX
SCAN_STATE = DATA_DIR / "vault_scan.json"
REVIEW_BASE = "Lobster review.base"
TITLE_LIMIT = 80
BAD_CHARS = re.compile(r'[\\/:*?"<>|#^\[\]{}\x00-\x1f]')
SKIP_DIRS = {".obsidian", ".trash", ".git"}


class VaultError(Exception):
    pass


# --- Names and properties -----------------------------------------------------------

def safe_title(text: str, limit: int = TITLE_LIMIT) -> str:
    """A title that's also a valid file name and wikilink target on every device."""
    t = " ".join(BAD_CHARS.sub(" ", str(text)).split()).strip(" .")
    if len(t) > limit:
        t = t[:limit].rsplit(" ", 1)[0].rstrip(" ,;.-")
    return t or "Untitled"


def unique_title(title: str) -> str:
    """The title, or "title (2)", "(3)"... if a note by that name is already in the inbox."""
    base, n = safe_title(title), 1
    while (INBOX / (f"{base}.md" if n == 1 else f"{base} ({n}).md")).exists():
        n += 1
    return base if n == 1 else f"{base} ({n})"


def link(title: str) -> str:
    return f"[[{safe_title(title, TITLE_LIMIT + 8)}]]"


def _yaml(value) -> str:
    if isinstance(value, list):
        return "[" + ", ".join(_yaml(v) for v in value) + "]"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    s = str(value)
    if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9 ._-]*", s) and s.lower() not in ("true", "false", "null", "yes", "no"):
        return s
    return json.dumps(s, ensure_ascii=False)            # a valid YAML double-quoted string


def frontmatter(props: dict) -> str:
    return "---\n" + "".join(f"{k}: {_yaml(v)}\n" for k, v in props.items()) + "---\n"


def read_frontmatter(path: Path, limit: int = 4096) -> dict:
    """Properties only (the body is never read back), as plain strings or lists."""
    try:
        with path.open("r", encoding="utf-8", errors="replace") as f:
            head = f.read(limit)
    except OSError:
        return {}
    if not head.startswith("---"):
        return {}
    end = head.find("\n---", 3)
    if end == -1:
        return {}
    props = {}
    for line in head[3:end].splitlines():
        key, sep, value = line.partition(":")
        if not sep or not re.fullmatch(r"[a-z_]+", key.strip()):
            continue
        value = value.strip()
        if value.startswith("[") and value.endswith("]"):
            props[key.strip()] = [v.strip().strip('"') for v in value[1:-1].split(",") if v.strip()]
        else:
            props[key.strip()] = value.strip('"')
    return props


# --- Writing ------------------------------------------------------------------------

def _inside(path: Path, root: Path) -> bool:
    root = root.resolve()
    return root in path.resolve().parents


def _write_new(title: str, text: str) -> Path:
    """Create a new note in the inbox: never replacing an existing file, never half-written.

    Works on every file system, including Android's (no hard links there):
      1. reserve the name: the OS creates it only if nobody has it (O_EXCL), else try " (2)"...
      2. write the note into a hidden temp file and flush it to disk
      3. swap the finished note into the reserved name in one step
    """
    INBOX.mkdir(parents=True, exist_ok=True)
    base, n = safe_title(title, TITLE_LIMIT + 8), 1      # room for a " (2)" from unique_title
    while True:
        path = INBOX / (f"{base}.md" if n == 1 else f"{base} ({n}).md")
        if not _inside(path, INBOX):
            raise VaultError("refused: outside the vault inbox")
        try:
            os.close(os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644))
            break
        except FileExistsError:
            n += 1
    tmp = INBOX / f".{path.name}.{secrets.token_hex(4)}.tmp"   # hidden: sync tools and Obsidian skip it
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, path)                    # replaces only our own empty placeholder
    except BaseException:
        tmp.unlink(missing_ok=True)
        if path.exists() and path.stat().st_size == 0:
            path.unlink()                        # our own empty placeholder, nothing of yours
        raise
    return path


def relative(path: Path) -> str:
    return str(path.resolve().relative_to(VAULT_DIR.resolve()))


def today() -> str:
    return time.strftime("%Y-%m-%d")


def write_card(card_id: str, title: str, claim: str, evidence: str, source_title: str, fetched: str,
               map_title: str, loci: list[str], about: str = "", question: str = "", section: str = "") -> Path:
    """Card guide v2 adds context: what the card is about, the question it answers and the part
    of the source it comes from. v1 cards are never rewritten."""
    props = {"id": card_id, "type": "card", "loci": loci, "status": "unreviewed", "made_by": "lobster",
             "sources": [link(source_title)], "maps": [link(map_title)], "created": today(),
             "card_version": CARD_VERSION + 1}
    if about:
        props["about"] = about
    if question:
        props["question"] = question[:200]
    if section:
        props["section"] = section[:80]
    quote = "\n".join(f"> {line}" for line in evidence.splitlines() if line.strip())
    where = " · ".join(p for p in (f"About **{about}**" if about else "", f"section *{section}*" if section else "") if p)
    context = (f"{where}\n" if where else "") + (f"Answers: {question[:200]}\n" if question else "")
    body = (f"# {safe_title(title, TITLE_LIMIT + 8)}\n\n" + (context + "\n" if context else "")
            + f"{claim}\n\n{quote}\n> — {link(source_title)}, fetched {fetched[:10]}\n")
    return _write_new(title, frontmatter(props) + body)


def write_source(source_id: str, title: str, url: str, fetched: str, card_titles: list[str],
                 fingerprint: str = "", published: str = "", copy: str = "") -> Path:
    props = {"id": source_id, "type": "source", "status": "unreviewed", "made_by": "lobster",
             "url": url, "fetched": fetched[:10], "created": today(), "card_version": CARD_VERSION}
    if published:
        props["published"] = published
    if fingerprint:
        props["sha256"] = fingerprint
    if copy:
        props["copy"] = copy                     # read from an offline Kiwix file, not the live page
    cited = "\n".join(f"- {link(t)}" for t in card_titles) or "- (no cards yet)"
    origin = f"from the offline copy {copy} (the live page is <{url}>)" if copy else f"from <{url}>"
    body = (f"# {safe_title(title, TITLE_LIMIT + 8)}\n\nCollected by the Spider {origin}. "
            f"This page is the record of what was read; it is never edited afterwards.\n\n"
            f"## Cards from this source\n{cited}\n")
    return _write_new(title, frontmatter(props) + body)


def write_map(map_id: str, title: str, question: str, job: int, points: list[tuple[str, int]],
              sources: list[str], loci: list[str], sections: list | None = None,
              bottom: list[str] | None = None) -> Path:
    """sections (optional): [(sub-question, summary, [(card title, source number)])], so the map
    follows the brief's outline; without them, one list of points."""
    props = {"id": map_id, "type": "map", "loci": loci, "status": "unreviewed", "made_by": "lobster",
             "question": question, "research_job": job, "created": today(), "card_version": CARD_VERSION}
    srcs = "\n".join(f"{i}. {link(s)}" for i, s in enumerate(sources, 1))
    if sections:
        blocks = []
        for q, summary, pts in sections:
            lines = "\n".join(f"- {link(t)} [{n}]" for t, n in pts)
            blocks.append(f"## {safe_title(q, 120)}\n" + (f"{summary}\n\n" if summary else "") + (lines or "_Little found._"))
        middle = (f"## Bottom line\n{' '.join(bottom)}\n\n" if bottom else "") + "\n\n".join(blocks)
    else:
        middle = "## Points\n" + "\n".join(f"- {link(t)} [{n}]" for t, n in points)
    body = (f"# {safe_title(title, TITLE_LIMIT + 8)}\n\nQuestion: {question}\n\n{middle}\n\n## Sources\n{srcs}\n\n"
            "Gathered by Sloth Lobster from the web, not written by you. Review each card before relying on it.\n")
    return _write_new(title, frontmatter(props) + body)


def write_note(note_id: str, text: str, loci: list[str]) -> Path:
    """A note you captured through Sloth Lobster: your own words, so made_by is you."""
    first = re.split(r"(?<=[.!?])\s", text.strip(), 1)[0]
    props = {"id": note_id, "type": "note", "loci": loci, "status": "verified", "made_by": "you",
             "captured_by": "lobster", "created": today(), "card_version": CARD_VERSION}
    return _write_new(first, frontmatter(props) + f"# {safe_title(first)}\n\n{text.strip()}\n")


def log(line: str) -> None:
    """Append one line to today's Lobster log: what was written, so rooms can see what's new."""
    INBOX.mkdir(parents=True, exist_ok=True)
    path = INBOX / f"Lobster log {today()}.md"
    old = path.read_text(encoding="utf-8") if path.exists() else (
        frontmatter({"type": "log", "made_by": "lobster", "created": today(), "card_version": CARD_VERSION})
        + f"# Lobster log {today()}\n\n")
    write_atomic(path, old + f"- {time.strftime('%H:%M')} {line}\n")


def ensure_review_view() -> None:
    """A ready-made Obsidian Bases view of every unreviewed Sloth Lobster card (written once, then yours)."""
    path = INBOX / REVIEW_BASE
    if path.exists():
        return
    INBOX.mkdir(parents=True, exist_ok=True)
    write_atomic(path, (
        "filters:\n  and:\n    - made_by == \"lobster\"\n    - status == \"unreviewed\"\n"
        "views:\n  - type: table\n    name: To review\n    order:\n"
        "      - file.name\n      - type\n      - loci\n      - sources\n      - created\n"))


# --- Reading reviews back -------------------------------------------------------------

def scan_reviews(full: bool = False) -> list[dict]:
    """Sloth Lobster notes changed since the last pass: [{id, status, path}]. Looks across the whole
    vault, so cards you promoted out of the inbox are still found (by id, not by name)."""
    if not VAULT_DIR.exists():
        return []
    try:
        state = json.loads(SCAN_STATE.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        state = {}
    since = 0.0 if full else float(state.get("last", 0.0))
    started = time.time()
    found = []
    for root, dirs, files in os.walk(VAULT_DIR):
        dirs[:] = [d for d in dirs if d not in SKIP_DIRS and not d.startswith(".")]
        for name in files:
            if not name.endswith(".md"):
                continue
            path = Path(root) / name
            try:
                if path.stat().st_mtime < since:
                    continue
            except OSError:
                continue
            props = read_frontmatter(path)
            if props.get("made_by") in ("lobster", "you") and str(props.get("id", "")).startswith("lb-"):
                found.append({"id": props["id"], "status": props.get("status", ""), "path": relative(path)})
    if not full:                                # only the regular pass moves the bookmark
        write_atomic(SCAN_STATE, json.dumps({"last": started - 2}))   # small overlap: never miss an edit
    return found


def set_status(rel_path: str, card_id: str, status: str) -> bool:
    """Change only the status line of one Sloth Lobster note, when you ask. Refuses anything else."""
    path = (VAULT_DIR / rel_path)
    if not _inside(path, VAULT_DIR) or not path.is_file():
        return False
    props = read_frontmatter(path)
    if props.get("id") != card_id or props.get("made_by") != "lobster":
        return False
    text = path.read_text(encoding="utf-8")
    end = text.find("\n---", 3)
    head, rest = text[:end], text[end:]
    new_head, n = re.subn(r"(?m)^status:.*$", f"status: {status}", head, count=1)
    if n != 1:
        return False
    write_atomic(path, new_head + rest)
    return True


def find(card_id: str) -> str | None:
    """Where a note with this id lives now (you may have renamed or moved it)."""
    for item in scan_reviews(full=True):
        if item["id"] == card_id:
            return item["path"]
    return None
