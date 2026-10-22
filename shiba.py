"""🐕 Shiba: the watchdog. Plain code, never a model.

Every action, approval, link and web fact passes through here.

  ALLOW -> run it
  ASK   -> needs your tap on a one-time approval card
  BLOCK -> refused, with a reason

Every decision is written to data/audit.jsonl, so you can always see
what happened and why. Shiba's own rules only change when you edit this
file, the tests pass, and you re-seal with `python seal.py`.
"""
import hashlib
import ipaddress
import json
import re
import secrets
import socket
import time
import unicodedata
from collections import deque
from urllib.parse import urlparse

from config import BASE_DIR, DATA_DIR, PROXY
from store import append_jsonl, read_jsonl, write_atomic

ALLOW, ASK, BLOCK = "ALLOW", "ASK", "BLOCK"

AUDIT = DATA_DIR / "audit.jsonl"
PAUSE_FLAG = DATA_DIR / "PAUSED"
APPROVALS = DATA_DIR / "approvals.json"
SEAL_FILE = DATA_DIR / "seal.json"
SEALED_FILES = ["identity.txt", "actions.py", "shiba.py", "web.py"]

APPROVAL_TTL = 600            # seconds an approval card stays valid
MAX_TEXT = 500                # longest text an action may receive
MAX_MESSAGES_PER_MINUTE = 20
ALERT_EVERY = 600             # at most one alert per 10 minutes
AUDIT_MAX_BYTES = 5_000_000   # then the log is rotated (renamed, never deleted)
SUSPICIOUS_PAGE = 3           # this many rejected facts from one page -> alert

TEST_ALLOW_LOOPBACK = False   # only the test harness flips this, in-process


# --- Audit log -------------------------------------------------------------------

def audit(kind: str, **fields) -> None:
    """Append one line to the audit log. Never raises: logging must not break Sloth Lobster."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if AUDIT.exists() and AUDIT.stat().st_size > AUDIT_MAX_BYTES:
            AUDIT.rename(AUDIT.with_name(f"audit-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"))
        record = {"kind": kind, **{k: (v[:200] if isinstance(v, str) else v) for k, v in fields.items()}}
        append_jsonl(AUDIT, {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), **record}, stamp=None)
    except Exception:
        pass


def recent(n: int = 8) -> list[dict]:
    return read_jsonl(AUDIT)[-n:]


def blocks_last_day() -> int:
    cutoff = time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(time.time() - 86400))
    return sum(1 for r in recent(3000)
               if r.get("ts", "") >= cutoff
               and (r.get("verdict") == BLOCK or r["kind"] in ("fact_rejected", "fetch_refused", "redirect_blocked")))


# --- Pause switch ----------------------------------------------------------------

def paused() -> bool:
    return PAUSE_FLAG.exists()


def pause() -> str:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PAUSE_FLAG.write_text(time.strftime("%Y-%m-%d %H:%M:%S"), encoding="utf-8")
    audit("paused")
    return "🐕 Paused. Every write waits; reading and /status still work. /resume when you're ready."


def resume() -> str:
    if PAUSE_FLAG.exists():
        PAUSE_FLAG.unlink()
    audit("resumed")
    return "▶️ Resumed."


# --- Rate limit ------------------------------------------------------------------

_recent_messages: deque = deque()


def message_allowed() -> bool:
    now = time.time()
    while _recent_messages and now - _recent_messages[0] > 60:
        _recent_messages.popleft()
    if len(_recent_messages) >= MAX_MESSAGES_PER_MINUTE:
        audit("rate_limited")
        return False
    _recent_messages.append(now)
    return True


# --- Action gate -----------------------------------------------------------------

CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


def text_problem(text: str) -> str | None:
    if len(text) > MAX_TEXT:
        return f"text is longer than {MAX_TEXT} characters"
    if CONTROL_CHARS.search(text):
        return "text contains control characters"
    return None


def decide(action: str, text: str, tainted: bool) -> tuple[str, str]:
    """The whole action policy in one place."""
    from actions import ACTIONS  # late import: actions imports shiba too
    spec = ACTIONS.get(action)
    if spec is None:
        verdict, reason = BLOCK, "not on the allowlist"
    elif paused() and spec["tier"] != "read":
        verdict, reason = BLOCK, "Sloth Lobster is paused"
    elif (problem := text_problem(text)):
        verdict, reason = BLOCK, problem
    elif spec["tier"] == "confirm":
        verdict, reason = ASK, "this action always asks"
    elif spec["tier"] == "write" and tainted:
        verdict, reason = ASK, "web facts are in this conversation"
    else:
        verdict, reason = ALLOW, "ok"
    audit("decision", action=action, text=text, tainted=tainted, verdict=verdict, reason=reason)
    return verdict, reason


# --- One-time approval cards -----------------------------------------------------

def _load_approvals() -> dict:
    try:
        return json.loads(APPROVALS.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def _save_approvals(data: dict) -> None:
    write_atomic(APPROVALS, json.dumps(data))


def new_approval(action: str, text: str) -> str:
    """Issue a one-time code bound to exactly this action and text."""
    now = time.time()
    data = {c: r for c, r in _load_approvals().items() if r["expires"] > now}
    code = secrets.token_urlsafe(9)
    data[code] = {"action": action, "text": text, "expires": now + APPROVAL_TTL}
    _save_approvals(data)
    audit("approval_issued", action=action, code=code[:4])
    return code


def redeem(code: str) -> tuple[dict | None, str]:
    data = _load_approvals()
    rec = data.pop(code, None)
    _save_approvals(data)
    if rec is None:
        audit("approval_refused", code=code[:4], reason="unknown or already used")
        return None, "That approval is unknown or was already used. Nothing was changed."
    if rec["expires"] < time.time():
        audit("approval_refused", code=code[:4], reason="expired")
        return None, "That approval expired. Ask me again. Nothing was changed."
    if paused():
        audit("approval_refused", code=code[:4], reason="paused")
        return None, "Sloth Lobster is paused. Nothing was changed."
    audit("approval_redeemed", action=rec["action"], code=code[:4])
    return rec, ""


def cancel(code: str) -> None:
    data = _load_approvals()
    if data.pop(code, None) is not None:
        _save_approvals(data)
    audit("approval_cancelled", code=code[:4])


# --- Link guard (used by the Spider) ---------------------------------------------

def _ip_problem(ip_text: str) -> str | None:
    ip = ipaddress.ip_address(ip_text.split("%")[0])
    if TEST_ALLOW_LOOPBACK and ip.is_loopback:
        return None
    if not ip.is_global or ip.is_multicast or ip.is_reserved:
        return f"it points to a private or reserved address ({ip})"
    return None


def url_problem(url: str, allow_unresolved: bool = False) -> str | None:
    """Why this link must not be fetched, or None if it's fine.

    Checks the scheme, refuses credentials in links, and resolves the host so
    that names pointing at the tablet or your home network are refused too.
    """
    try:
        p = urlparse(url)
    except ValueError:
        return "it isn't a valid link"
    if p.scheme not in ("http", "https"):
        return "only http:// or https:// links are allowed"
    if not p.hostname:
        return "the link has no host"
    if p.username or p.password:
        return "links with passwords are not allowed"
    try:
        return _ip_problem(p.hostname)            # the host is an IP literal
    except ValueError:
        pass                                      # it's a name: resolve it
    try:
        port = p.port or (443 if p.scheme == "https" else 80)
        infos = socket.getaddrinfo(p.hostname, port, proto=socket.IPPROTO_TCP)
    except (socket.gaierror, UnicodeError, ValueError):
        # Offline, or behind a proxy that resolves names for us: checked again at fetch.
        return None if (allow_unresolved or PROXY) else "the host can't be found"
    for info in infos:
        problem = _ip_problem(info[4][0])
        if problem:
            return problem
    return None


# --- Fact filter (used by the Elephant) ------------------------------------------

# Two kinds of rule. HARD patterns never belong in an encyclopedia fact. CONTEXT
# patterns catch words that are innocent alone ("secretly married", "a token of
# friendship", "the Zimmermann Telegram") only when they're used the way an
# injection uses them: addressed to an agent, asking for credentials, giving orders.
AGENTS = r"(?:lobster|elephant|spider|shiba|assistant|ai|bot|agent|model)"
SUSPICIOUS = re.compile("|".join([
    # hard
    r"https?://|www\.|[<>{}`$\\]|@\w{3,}",
    r"\bsystem prompt\b|\bapi[ _-]?keys?\b|\bpasswords?\b|\bseed phrase\b|\bprivate key\b",
    r"\byou (?:are|must|should|will|need)\b|\bdo not tell\b|\bfrom now on\b",
    # orders to forget or ignore the rules
    r"\b(?:ignore|disregard|forget|override)\b(?:\W+\w+){0,3}?\W+(?:instructions?|rules|prompts?|guidelines|"
    r"directions|messages|above|everything|previous|prior)\b",
    r"\b(?:new|updated|following|hidden|secret) instructions?\b",
    # speaking to an agent, or about its owner
    rf"^\W*{AGENTS}\s*:|\b(?:the|your|this)\s+{AGENTS}s?\s+(?:must|should|will|shall|needs? to|has to|is to|can now)\b",
    rf"\b(?:ai|virtual|digital)\s+{AGENTS}\b|\btell the {AGENTS}\b",
    r"\byour owner\b|\bthe owner'?s (?:notes|files|data|tasks|account|key|messages|memory)\b",
    # credentials and tricks
    r"\b(?:bot|api|access|auth|bearer|session|login|telegram) tokens?\b|\btokens? (?:here|below)\b",
    r"\bsecretly (?:send|tell|forward|add|save|remember|share|copy|post|upload|delete)\b|\bkeep (?:this|it) secret\b",
    r"\btelegram (?:account|bot|chat|user|channel|group)\b|\bon telegram\b",
    r"\b(?:crypto|bitcoin|ethereum|wallet) (?:wallet|address|keys?)\b|\bgift cards?\b|\bwire money\b",
    r"\bexecute (?:this|the following|the command|the code|the script|it now)\b|\brun (?:this|the following)\b",
    r"\bclick (?:here|this|the link|below)\b|\bdownload (?:this|the file|it now|here)\b",
    r"\bsend (?:it|this|me|your|the (?:seed|key|code|token|notes|file))\b|\bforward (?:all|every|your)\b",
]), re.I)

# Look-alike letters an attacker might use to sneak past the rules above.
HOMOGLYPHS = str.maketrans("аеорсухіјѕԁɡοΑΒΕΗΙΚΜΝΟΡΤΧΥΖ", "aeopcyxijsdgoABEHIKMNOPTXYZ")
INVISIBLE = re.compile(r"[\u00ad\u200b-\u200f\u202a-\u202e\u2060-\u2064\ufeff]")


def normalize(text: str) -> str:
    """Fold full-width letters, look-alikes and invisible characters into plain text."""
    return INVISIBLE.sub("", unicodedata.normalize("NFKC", text)).translate(HOMOGLYPHS)


def fact_problem(fact: str) -> str | None:
    fact = fact.strip()
    if not 20 <= len(fact) <= 300:
        return "wrong length"
    m = SUSPICIOUS.search(normalize(fact))
    return f"matched {m.group(0)!r}" if m else None


# --- Grounding: a web fact must match the text it came from ------------------------
# A small model sometimes garbles while extracting: a wrong year, a mixed-up name.
# Every number in a fact must appear in its supporting sentence, and every name in
# that sentence or the ones beside it. Otherwise the fact is rejected and logged.

NUMBER = re.compile(r"\d[\d,]*(?:\.\d+)?")
NAME = re.compile(r"\b[A-Z][\w'’-]*(?:\s+(?:of|de|da|di|du|van|von|ap|al|bin|ibn|la|le|the|y)?\s*[A-Z][\w'’-]*)*")
CONNECTORS = {"of", "de", "da", "di", "du", "van", "von", "ap", "al", "bin", "ibn", "la", "le", "the", "y"}
SENTENCE_STARTS = {"the", "a", "an", "in", "on", "at", "by", "for", "from", "with", "under", "during", "after",
                   "before", "when", "while", "as", "he", "she", "it", "his", "her", "its", "they", "their",
                   "this", "that", "these", "those", "there", "both", "some", "many", "most", "one", "two"}


def _plain(text: str) -> str:
    return " ".join(text.replace("’", "'").replace(",", "").lower().split())


def grounding_problem(fact: str, sentence: str, window: str) -> str | None:
    """Why this fact doesn't match its source, or None if it does."""
    s, w = _plain(sentence), _plain(window)
    for raw in NUMBER.findall(fact):
        n = raw.replace(",", "").rstrip(".")
        if not re.search(rf"(?<![\d.]){re.escape(n)}(?![\d])", s):
            return f"number {n} isn't in its source sentence"
    for m in NAME.finditer(fact):
        words = [x for x in m.group(0).replace("’", "'").split()]
        while words and words[0].lower() in SENTENCE_STARTS:
            words = words[1:]
        words = [re.sub(r"'s$", "", x) for x in words]
        core = [x for x in words if x.lower() not in CONNECTORS]
        if not core:
            continue
        if len(core) == 1:                        # one word: allow other endings (Catholic/Catholicism)
            stem = core[0].lower()[:6]
            if not re.search(rf"\b{re.escape(stem)}", w):
                return f"name {core[0]!r} isn't in its source"
        elif _plain(" ".join(words)) not in w:    # a full name must appear as written
            return f"name {' '.join(words)!r} isn't in its source"
    return None


# --- Outbound check (what Sloth Lobster sends you) -------------------------------------

URL_IN_TEXT = re.compile(r"https?://\S+|www\.\S+", re.I)


def strip_links(text: str) -> str:
    """Model-written text may not carry links; real sources are listed by code."""
    kept = [line for line in text.splitlines() if not URL_IN_TEXT.search(line)]
    if len(kept) != len(text.splitlines()):
        audit("links_stripped", removed=len(text.splitlines()) - len(kept))
    return "\n".join(kept)


# --- Alerts ----------------------------------------------------------------------

_last_alert = 0.0


def should_alert() -> bool:
    global _last_alert
    now = time.time()
    if now - _last_alert >= ALERT_EVERY:
        _last_alert = now
        return True
    return False


# --- Seal ------------------------------------------------------------------------

def _hashes() -> dict:
    return {name: hashlib.sha256((BASE_DIR / name).read_bytes()).hexdigest() for name in SEALED_FILES}


def write_seal() -> str:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    SEAL_FILE.write_text(json.dumps(_hashes(), indent=1), encoding="utf-8")
    audit("sealed", files=", ".join(SEALED_FILES))
    return "🐕 Sealed: " + ", ".join(SEALED_FILES)


def seal_problem() -> str | None:
    if not SEAL_FILE.exists():
        return "the rules are not sealed yet"
    try:
        sealed = json.loads(SEAL_FILE.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return "the seal file is damaged"
    changed = [n for n, h in _hashes().items() if sealed.get(n) != h]
    return f"changed since the last seal: {', '.join(changed)}" if changed else None
