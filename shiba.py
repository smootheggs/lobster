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
from collections import deque
from urllib.parse import urlparse

from config import BASE_DIR, DATA_DIR, PROXY

ALLOW, ASK, BLOCK = "ALLOW", "ASK", "BLOCK"

AUDIT = DATA_DIR / "audit.jsonl"
PAUSE_FLAG = DATA_DIR / "PAUSED"
APPROVALS = DATA_DIR / "approvals.json"
SEAL_FILE = DATA_DIR / "seal.json"
SEALED_FILES = ["identity.txt", "actions.py", "shiba.py"]

APPROVAL_TTL = 600            # seconds an approval card stays valid
MAX_TEXT = 500                # longest text an action may receive
MAX_MESSAGES_PER_MINUTE = 20
ALERT_EVERY = 600             # at most one alert per 10 minutes
AUDIT_MAX_BYTES = 5_000_000   # then the log is rotated (renamed, never deleted)
SUSPICIOUS_PAGE = 3           # this many rejected facts from one page -> alert

TEST_ALLOW_LOOPBACK = False   # only the test harness flips this, in-process


# --- Audit log -------------------------------------------------------------------

def audit(kind: str, **fields) -> None:
    """Append one line to the audit log. Never raises: logging must not break Lobster."""
    try:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if AUDIT.exists() and AUDIT.stat().st_size > AUDIT_MAX_BYTES:
            AUDIT.rename(AUDIT.with_name(f"audit-{time.strftime('%Y%m%d-%H%M%S')}.jsonl"))
        record = {"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "kind": kind}
        record.update({k: (v[:200] if isinstance(v, str) else v) for k, v in fields.items()})
        with AUDIT.open("a", encoding="utf-8") as f:
            f.write(json.dumps(record, ensure_ascii=False) + "\n")
    except Exception:
        pass


def recent(n: int = 8) -> list[dict]:
    if not AUDIT.exists():
        return []
    lines = AUDIT.read_text(encoding="utf-8").splitlines()[-n:]
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


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
    return "⏸️ Paused. Every write is blocked; reading and /status still work. /resume when ready."


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
        verdict, reason = BLOCK, "Lobster is paused"
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
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    tmp = APPROVALS.with_suffix(".tmp")
    tmp.write_text(json.dumps(data), encoding="utf-8")
    tmp.replace(APPROVALS)


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
        return None, "Lobster is paused. Nothing was changed."
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

SUSPICIOUS = re.compile(
    r"https?://|www\.|\bignore\b|\bdisregard\b|\bforget\b|\bsystem prompt\b|\binstruction|"
    r"\byou (are|must|should|will|need)\b|\bassistant\b|\bai (model|system|agent)\b|"
    r"\blobster\b|\belephant\b|\btelegram\b|\bpassword|\bapi key|\btoken\b|\bexecute\b|"
    r"\brun (the|this)\b|\bclick\b|\bsend (it|this|to|me|your)\b|\bdownload\b|\bwallet\b|"
    r"[<>{}`$\\]|\bdo not tell\b|\bsecret",
    re.I,
)


def fact_problem(fact: str) -> str | None:
    fact = fact.strip()
    if not 20 <= len(fact) <= 300:
        return "wrong length"
    m = SUSPICIOUS.search(fact)
    return f"matched {m.group(0)!r}" if m else None


# --- Outbound check (what Lobster sends you) -------------------------------------

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
