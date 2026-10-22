"""Sloth Lobster's app: the same house, with several doors.

The app on your phones and on the tablet talks to this small server, which runs inside the
bot's process and calls the same conversation engine as Telegram, so every message gets
exactly the same Shiba checks and one-time approval cards.

The locks (all here, all plain code, sealed with Shiba's rules):
  - doors only on private addresses: the tablet itself, Tailscale, and Wi-Fi networks you
    named (WEB_TRUSTED_WIFI); never a public or mobile-data address, never 0.0.0.0 by accident
  - HTTPS with Sloth Lobster's own certificate (certs.py), so an app pretending to be
    Sloth Lobster can't receive your password or login
  - only answers requests addressed to its own addresses (stops DNS-rebinding tricks)
  - the password is kept only as a scrypt hash, set by set_password.py
  - 5 wrong passwords lock the login for 15 minutes, with an audit line and an alert
  - sessions: random token, HttpOnly + SameSite=Strict (+ Secure) cookie, 12 hours, stored
    only as hashes; "log out everywhere"; a new password ends every session
  - every change needs the session's anti-forgery (CSRF) token and a JSON body
  - strict security headers on every response; files can't escape web/ or the Library
"""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import os
import secrets
import shutil
import ssl
import sys
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import actions
import certs
import config
import conversation
import elephant
import memory
import ollama
import research
import shiba
import spider
from store import append_jsonl, read_jsonl, timing_summary

log = logging.getLogger("lobster.web")

WEB_DIR = config.BASE_DIR / "web"
SESSION_SECONDS = 12 * 3600
MAX_FAILS = 5
LOCK_SECONDS = 15 * 60
MAX_BODY = 16_000
MAX_CHAT = 2_000
COOKIE = "lobster_session"

STATIC_TYPES = {".html": "text/html; charset=utf-8", ".js": "text/javascript; charset=utf-8",
                ".css": "text/css; charset=utf-8", ".webmanifest": "application/manifest+json",
                ".svg": "image/svg+xml", ".png": "image/png"}
SECURITY_HEADERS = {
    "Content-Security-Policy": ("default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; "
                                "connect-src 'self'; manifest-src 'self'; worker-src 'self'; object-src 'none'; "
                                "base-uri 'none'; form-action 'none'; frame-ancestors 'none'"),
    "X-Frame-Options": "DENY",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
    "Cross-Origin-Opener-Policy": "same-origin",
    "Cross-Origin-Resource-Policy": "same-origin",
    "Permissions-Policy": "camera=(), microphone=(), geolocation=(), payment=()",
}


# --- Password (scrypt) -----------------------------------------------------------------

def hash_password(password: str, n: int = 2 ** 14, r: int = 8, p: int = 1) -> str:
    salt = secrets.token_bytes(16)
    key = hashlib.scrypt(password.encode(), salt=salt, n=n, r=r, p=p, maxmem=64 * 1024 * 1024, dklen=32)
    return f"scrypt${n}${r}${p}${salt.hex()}${key.hex()}"


def verify_password(password: str, stored: str) -> bool:
    try:
        kind, n, r, p, salt, key = stored.split("$")
        if kind != "scrypt":
            return False
        got = hashlib.scrypt(password.encode(), salt=bytes.fromhex(salt), n=int(n), r=int(r), p=int(p),
                             maxmem=64 * 1024 * 1024, dklen=len(key) // 2)
    except (ValueError, TypeError):
        return False
    return hmac.compare_digest(got.hex(), key)


# --- Where the door may open --------------------------------------------------------------

def bind_problem(bind: str, allow_all: bool = False) -> str | None:
    if not bind:
        return "WEB_BIND is empty, so the web app is off"
    try:
        ip = ipaddress.ip_address(bind)
    except ValueError:
        return f"WEB_BIND must be an IP address, not {bind!r}"
    if ip.is_unspecified and not allow_all:
        return ("listening on every network (0.0.0.0) is refused. Use the tablet's hotspot or Tailscale IP, "
                "or set WEB_ALLOW_ALL=1 if you really mean it")
    return None


def allowed_hosts(bind: str, extra: list[str]) -> set[str]:
    hosts = {bind.lower(), *extra}
    if ipaddress.ip_address(bind).is_loopback:
        hosts |= {"localhost", "127.0.0.1"}
    return hosts


def confined(root: Path, rel: str) -> Path | None:
    """The file `rel` inside `root`, or None if it would step outside it (../, links, NUL)."""
    rel = unquote(rel)
    if "\x00" in rel or "\\" in rel:
        return None
    root = root.resolve()
    try:
        path = (root / rel.lstrip("/")).resolve()
    except (OSError, ValueError):
        return None
    if root not in path.parents or not path.is_file():
        return None
    if any(part.startswith(".") for part in path.relative_to(root).parts):
        return None
    return path


# --- Sessions and the lockout ------------------------------------------------------------

class Gate:
    """Logins, sessions and the lockout. Thread-safe.

    Sessions survive a restart: only a SHA-256 of each login token is stored (data/web_sessions.json,
    owner-only), and the anti-forgery token is derived from the login token with a server secret,
    so the file holds nothing that lets anyone in. Changing the password ends every session.
    """

    def __init__(self, password_hash: str, clock=time.time, notify=None, store: Path | None = None):
        self.password_hash = password_hash
        self.clock = clock
        self.notify = notify or (lambda text: None)
        self.lock = threading.Lock()
        self.store = store
        self.fp = hashlib.sha256(password_hash.encode()).hexdigest()[:16]
        self.secret = self._secret()
        self.sessions: dict[str, float] = self._load()      # token hash -> expiry
        self.fails = 0
        self.locked_until = 0.0

    # --- storage ---
    def _secret(self) -> bytes:
        if self.store is None:
            return secrets.token_bytes(32)
        path = self.store / "web_secret"
        try:
            return bytes.fromhex(path.read_text().strip())
        except (FileNotFoundError, ValueError):
            path.parent.mkdir(parents=True, exist_ok=True)
            key = secrets.token_bytes(32)
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
            with os.fdopen(fd, "w") as f:
                f.write(key.hex())
            return key

    def _load(self) -> dict[str, float]:
        if self.store is None:
            return {}
        try:
            data = json.loads((self.store / "web_sessions.json").read_text())
        except (FileNotFoundError, json.JSONDecodeError):
            return {}
        if data.get("fp") != self.fp:                        # the password changed: everyone logs in again
            return {}
        now = self.clock()
        return {h: float(e) for h, e in data.get("sessions", {}).items() if float(e) > now}

    def _save(self) -> None:
        if self.store is None:
            return
        path = self.store / "web_sessions.json"
        tmp = path.with_name(".web_sessions.tmp")
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            json.dump({"fp": self.fp, "sessions": self.sessions}, f)
        os.replace(tmp, path)

    @staticmethod
    def _h(token: str) -> str:
        return hashlib.sha256(token.encode()).hexdigest()

    def _view(self, token: str, expires: float) -> dict:
        csrf = hmac.new(self.secret, token.encode(), hashlib.sha256).hexdigest()
        return {"token": token, "csrf": csrf, "expires": expires}

    # --- logins ---
    def login(self, password: str, source: str = "the network") -> tuple[dict | None, str]:
        with self.lock:
            now = self.clock()
            if now < self.locked_until:
                shiba.audit("web_login", ok=False, reason="locked", source=source)
                return None, f"Too many wrong passwords. Try again in {int(self.locked_until - now) // 60 + 1} minutes."
            ok = bool(self.password_hash) and verify_password(password, self.password_hash)
            shiba.audit("web_login", ok=ok, source=source)
            if not ok:
                self.fails += 1
                if self.fails >= MAX_FAILS:
                    self.fails, self.locked_until = 0, now + LOCK_SECONDS
                    shiba.audit("web_lockout", minutes=LOCK_SECONDS // 60, source=source)
                    self.notify(f"🐕 Shiba: {MAX_FAILS} wrong passwords on the app, from {source}. "
                                f"The login is locked for {LOCK_SECONDS // 60} minutes.")
                return None, "Wrong password."
            self.fails = 0
            token = secrets.token_urlsafe(32)
            self.sessions = {h: e for h, e in self.sessions.items() if e > now}
            self.sessions[self._h(token)] = now + SESSION_SECONDS
            self._save()
            return self._view(token, now + SESSION_SECONDS), "ok"

    def session(self, token: str | None) -> dict | None:
        if not token:
            return None
        with self.lock:
            h = self._h(token)
            e = self.sessions.get(h)
            if e and e > self.clock():
                return self._view(token, e)
            if h in self.sessions:
                self.sessions.pop(h)
                self._save()
            return None

    def logout(self, token: str) -> None:
        with self.lock:
            self.sessions.pop(self._h(token), None)
            self._save()

    def logout_all(self) -> int:
        with self.lock:
            n = len(self.sessions)
            self.sessions = {}
            self._save()
        shiba.audit("web_logout_all", sessions=n)
        return n


# --- The app: routes -----------------------------------------------------------------------

NOTE_IDS = config.DATA_DIR / "web_note_ids.jsonl"


class App:
    """The app's API. Every route calls the same functions Telegram does (conversation.py)."""

    def __init__(self, gate: Gate, loop: asyncio.AbstractEventLoop, hosts: set[str],
                 busy=lambda: False, secure: bool = False, now=lambda: "idle"):
        self.gate, self.loop, self.hosts, self.busy, self.secure, self.now = gate, loop, hosts, busy, secure, now
        self.doors: "Doors | None" = None
        self.note_lock = threading.Lock()
        # held notes from phones: each id saves exactly once, even across a restart
        self.note_ids: dict[str, str] = {r["id"]: r.get("reply", "Saved.") for r in read_jsonl(NOTE_IDS)[-500:]
                                         if isinstance(r, dict) and r.get("id")}

    def run(self, coro, timeout: int = 900):
        """Run a conversation step on the bot's event loop, where Telegram's run too."""
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    @staticmethod
    def _text(body, key: str, limit: int = MAX_CHAT) -> str | None:
        v = body.get(key)
        return v.strip()[:limit] if isinstance(v, str) else None

    # GET /api/...
    def me(self, s, q):
        return {"csrf": s["csrf"], "version": config.VERSION, "history": conversation.load_session()["history"]}

    def status(self, s, q):
        st = memory.stats()
        t = timing_summary()
        return {"model_up": ollama.alive(), "model": config.MODEL, "owner_facts": st["owner"],
                "web_facts": st["web"], "cards": st["cards"], "unreviewed": st["unreviewed"],
                "paused": shiba.paused(), "blocks_24h": shiba.blocks_last_day(),
                "research_waiting": len(research.pending()), "now": self.now(), "busy": self.busy(),
                "last_backup": memory.last_backup(), "research_seconds": t.get("research"),
                "online": spider.online(), "ollama": ollama.version_problem(),
                "doors": self.doors.urls() if self.doors else [],
                "wifi": self.doors.wifi_note if self.doors and "wifi" in self.doors.entries else None,
                "name": config.APP_NAME, "version": config.VERSION}

    def research_list(self, s, q):
        return {"jobs": research.history(30)}

    def cards_list(self, s, q):
        status = q.get("status", [""])[0]
        job = q.get("job", [""])[0]
        return {"cards": memory.cards("card", status if status in memory.STATUSES else None,
                                      int(job) if job.isdigit() else None, limit=100)}

    def library(self, s, q):
        items = []
        if config.LIBRARY_DIR.exists():
            for f in sorted(config.LIBRARY_DIR.iterdir(), key=lambda f: f.stat().st_mtime, reverse=True):
                if f.is_file() and not f.name.startswith(".") and not f.name.endswith(".receipt.json"):
                    receipt = {}
                    r = f.with_name(f.name + ".receipt.json")
                    if r.exists():
                        try:
                            receipt = json.loads(r.read_text(encoding="utf-8"))
                        except json.JSONDecodeError:
                            pass
                    items.append({"name": f.name, "bytes": f.stat().st_size, "url": receipt.get("url", ""),
                                  "saved": str(receipt.get("fetched_at", ""))[:16].replace("T", " "),
                                  "sha256": receipt.get("sha256", "")})
        return {"items": items[:200]}

    def memory_search(self, s, q):
        query = q.get("q", [""])[0][:200]
        hits = elephant.ask(query)["hits"] if query.strip() else []
        notes = [n.get("text", "") for n in read_jsonl(actions.NOTES)[-10:]][::-1]
        return {"facts": [dict(h, source_title=elephant.source_title(h["source"]) if h["tag"] == "web" else "")
                          for h in hits], "notes": notes}

    def audit_list(self, s, q):
        return {"rows": shiba.recent(20)[::-1]}

    # POST /api/...
    def chat(self, s, body):
        text = body.get("text")
        if not isinstance(text, str) or not text.strip():
            return {"reply": "Say something first.", "code": None}
        if len(text) > MAX_CHAT:
            return {"reply": f"That's longer than {MAX_CHAT} characters. Try a shorter message.", "code": None}
        if not shiba.message_allowed():
            return {"reply": "🐕 Slow down a little: too many messages this minute.", "code": None}
        if text == "/note" or text.startswith("/note "):
            return {"reply": conversation.note(text[5:].strip()), "code": None}
        busy = "🐘 The Elephant is digesting, so replies may be slow." if self.busy() else None
        r = self.run(conversation.handle(text, busy=busy))
        return {"reply": r["reply"], "code": r["code"], "ask": r.get("ask"), "action": r.get("action")}

    def approve(self, s, body):
        return {"reply": self.run(conversation.approve(str(body.get("code", ""))))}

    def cancel(self, s, body):
        return {"reply": conversation.cancel(str(body.get("code", "")))}

    def research_new(self, s, body):
        return {"reply": conversation.research(self._text(body, "question", 500) or "")}

    def ask(self, s, body):
        q = self._text(body, "question", 500) or ""
        return {"reply": conversation.ask(q), "hits": self.memory_search(s, {"q": [q]})["facts"] if q else []}

    def note(self, s, body):
        """Held notes from a phone carry an id, so a resend after a dropped connection saves once."""
        note_id = str(body.get("id", ""))[:64]
        if not note_id:
            return {"reply": conversation.note(self._text(body, "text") or "")}
        with self.note_lock:
            if note_id in self.note_ids:
                return {"reply": self.note_ids[note_id], "duplicate": True}
            was_paused = shiba.paused()
            reply = conversation.note(self._text(body, "text") or "")
            if not was_paused:                          # a paused note wasn't saved: it may be sent again
                self.note_ids[note_id] = reply
                append_jsonl(NOTE_IDS, {"id": note_id, "reply": reply[:300]})
                if len(self.note_ids) > 1000:
                    self.note_ids = dict(list(self.note_ids.items())[-500:])
        return {"reply": reply}

    def save(self, s, body):
        return {"reply": conversation.save_link(self._text(body, "url", 1000) or "")}

    def stop(self, s, body):
        return {"reply": conversation.stop()}

    def new(self, s, body):
        return {"reply": conversation.new_chat()}

    def review(self, s, body):
        card_id, status = str(body.get("id", ""))[:40], str(body.get("status", ""))
        if status not in ("verified", "wrong", "unreviewed"):
            return {"ok": False, "reply": "Unknown review."}
        if shiba.paused():
            return {"ok": False, "reply": f"⏸️ {config.APP_NAME} is paused. Send /resume first."}
        ok = elephant.review(card_id, status)
        return {"ok": ok, "reply": f"Marked {status}." if ok else "Couldn't find that card in your vault."}

    def pause(self, s, body):
        shiba.audit("web_command", command="pause")
        return {"reply": shiba.pause()}

    def resume(self, s, body):
        shiba.audit("web_command", command="resume")
        return {"reply": shiba.resume()}

    def logout(self, s, body):
        self.gate.logout(s["token"])
        return {"reply": "Logged out."}

    def logout_all(self, s, body):
        n = self.gate.logout_all()
        return {"reply": f"Logged out everywhere ({n} session{'s' if n != 1 else ''})."}

    GETS = {"/api/me": me, "/api/status": status, "/api/research": research_list, "/api/cards": cards_list,
            "/api/library": library, "/api/memory": memory_search, "/api/audit": audit_list}
    POSTS = {"/api/chat": chat, "/api/approve": approve, "/api/cancel": cancel, "/api/research": research_new,
             "/api/ask": ask, "/api/note": note, "/api/save": save, "/api/stop": stop, "/api/new": new,
             "/api/review": review, "/api/pause": pause, "/api/resume": resume, "/api/logout": logout,
             "/api/logout_all": logout_all}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "SlothLobster"
        timeout = 30                            # a stalled connection can't hold a thread forever
        sys_version = ""
        protocol_version = "HTTP/1.1"

        def log_message(self, fmt, *args):     # no URLs or bodies in the console
            pass

        # --- responses ---
        def _send(self, code: int, body: bytes = b"", ctype: str = "application/json",
                  extra: dict | None = None, cache: bool = False):
            self.send_response(code)
            for k, v in SECURITY_HEADERS.items():
                self.send_header(k, v)
            self.send_header("Cache-Control", "public, max-age=300" if cache else "no-store")
            self.send_header("Content-Type", ctype)
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Connection", "close")
            self.close_connection = True
            for k, v in (extra or {}).items():
                self.send_header(k, v)
            self.end_headers()
            if self.command != "HEAD":
                self.wfile.write(body)

        def _json(self, code: int, obj, extra: dict | None = None):
            self._send(code, json.dumps(obj, ensure_ascii=False).encode(), extra=extra)

        def _error(self, code: int, msg: str):
            self._json(code, {"error": msg})

        # --- checks ---
        def _host_ok(self) -> bool:
            host = (self.headers.get("Host") or "").lower()
            host = host[1:].split("]")[0] if host.startswith("[") else host.rsplit(":", 1)[0] if host.count(":") == 1 else host
            if host in app.hosts:
                return True
            shiba.audit("web_refused", reason="unknown host", host=host[:60])
            return False

        def _session(self):
            for part in (self.headers.get("Cookie") or "").split(";"):
                name, _, value = part.strip().partition("=")
                if name == COOKIE:
                    return app.gate.session(value)
            return None

        def _same_origin(self) -> bool:
            origin = self.headers.get("Origin")
            if origin is None:
                return True                      # same-origin fetches from older browsers, curl
            o = urlparse(origin)
            return (o.hostname or "").lower() in app.hosts

        def _read_body(self) -> bytes | None:
            """Read the request body first (so nothing is left on the line), or None if too big."""
            try:
                n = int(self.headers.get("Content-Length") or 0)
            except ValueError:
                n = -1
            if not 0 <= n <= MAX_BODY:
                return None
            return self.rfile.read(n)

        def _body(self, raw: bytes) -> dict | None:
            if "application/json" not in (self.headers.get("Content-Type") or ""):
                return None
            try:
                body = json.loads(raw or b"{}")
            except (json.JSONDecodeError, UnicodeDecodeError):
                return None
            return body if isinstance(body, dict) else None

        # --- verbs ---
        def do_GET(self):
            try:
                self._get()
            except Exception as e:
                self._crash(e)

        def do_POST(self):
            try:
                self._post()
            except Exception as e:
                self._crash(e)

        def _crash(self, e: Exception):
            if isinstance(e, (BrokenPipeError, ConnectionResetError, TimeoutError)):
                return                          # the phone hung up or left the Wi-Fi: not an error
            log.exception("web handler error")
            shiba.audit("handler_error", error=f"web: {type(e).__name__}")
            try:
                self._error(500, f"Something went wrong. {config.APP_NAME} is still running; details in the audit.")
            except Exception:
                pass

        def do_PUT(self):
            self._error(405, "method not allowed")
        do_DELETE = do_PATCH = do_OPTIONS = do_TRACE = do_CONNECT = do_PUT

        def _get(self):
            if not self._host_ok():
                return self._error(421, "wrong host")
            url = urlparse(self.path)
            if url.path.startswith("/api/"):
                s = self._session()
                if s is None:
                    return self._error(401, "log in first")
                route = App.GETS.get(url.path)
                if route is None:
                    return self._error(404, "no such page")
                return self._json(200, route(app, s, parse_qs(url.query)))
            if url.path.startswith("/library/"):
                if self._session() is None:
                    return self._error(401, "log in first")
                f = confined(config.LIBRARY_DIR, url.path[len("/library/"):])
                if f is None:
                    return self._error(404, "no such file")
                name = f.name.replace('"', "")
                return self._send(200, f.read_bytes(), "application/octet-stream",
                                  {"Content-Disposition": f'attachment; filename="{name}"'})
            rel = "index.html" if url.path in ("", "/") else url.path
            f = confined(WEB_DIR, rel)
            if f is None or f.suffix not in STATIC_TYPES:
                return self._error(404, "no such page")
            extra = {"Service-Worker-Allowed": "/"} if f.name == "sw.js" else None
            return self._send(200, f.read_bytes(), STATIC_TYPES[f.suffix], extra,
                              cache=f.suffix in (".png", ".svg"))

        def _post(self):
            raw = self._read_body()
            if raw is None:
                return self._error(413, "too big")
            if not self._host_ok():
                return self._error(421, "wrong host")
            if not self._same_origin():
                shiba.audit("web_refused", reason="cross-site request")
                return self._error(403, "cross-site request refused")
            path = urlparse(self.path).path
            body = self._body(raw)
            if body is None:
                return self._error(415, "send JSON")
            if path == "/api/login":
                source = "this tablet" if self.client_address[0].startswith("127.") else "the network"
                session, why = app.gate.login(str(body.get("password", ""))[:200], source)
                if session is None:
                    return self._error(403 if "Wrong" in why else 429, why)
                cookie = (f"{COOKIE}={session['token']}; HttpOnly; SameSite=Strict; Path=/; "
                          f"Max-Age={SESSION_SECONDS}" + ("; Secure" if app.secure else ""))
                return self._json(200, {"csrf": session["csrf"]}, {"Set-Cookie": cookie})
            s = self._session()
            if s is None:
                return self._error(401, "log in first")
            if not hmac.compare_digest(self.headers.get("X-CSRF-Token") or "", s["csrf"]):
                shiba.audit("web_refused", reason="missing or wrong CSRF token")
                return self._error(403, "missing anti-forgery token; reload the page")
            route = App.POSTS.get(path)
            if route is None:
                return self._error(404, "no such action")
            out = route(app, s, body)
            extra = ({"Set-Cookie": f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
                                     + ("; Secure" if app.secure else "")}
                     if path in ("/api/logout", "/api/logout_all") else None)
            return self._json(200, out, extra)

        do_HEAD = do_GET

    return Handler


# --- The doors: several addresses, one app ------------------------------------------------

def parse_doors(text: str) -> list[str]:
    """WEB_BIND, e.g. "local, 100.78.198.49, wifi" -> ["127.0.0.1", "100.78.198.49", "wifi"]."""
    out = []
    for part in (p.strip().lower() for p in text.split(",")):
        if part:
            out.append("127.0.0.1" if part in ("local", "localhost") else part)
    return list(dict.fromkeys(out))


def door_problem(entry: str, allow_all: bool = False) -> str | None:
    """Why this door must stay shut, or None. Only private addresses, never 0.0.0.0 by accident."""
    if entry == "wifi":
        return None
    problem = bind_problem(entry, allow_all)
    if problem:
        return problem
    if not allow_all and not certs.allowed_address(entry):
        return f"{entry} is not a private address (only the tablet, Tailscale and home or hotspot networks)"
    return None


def wifi_info() -> dict | None:
    """The tablet's Wi-Fi connection (Termux:API): {"ip", "ssid"}, or None when not on Wi-Fi.
    Reading the network name needs Termux:API's location permission; without it the name is
    unknown and the Wi-Fi door stays shut."""
    if not shutil.which("termux-wifi-connectioninfo"):
        return None
    try:
        out = subprocess.run(["termux-wifi-connectioninfo"], capture_output=True, text=True, timeout=15).stdout
        d = json.loads(out)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None
    if d.get("supplicant_state") != "COMPLETED" or not d.get("ip"):
        return None
    return {"ip": str(d["ip"]), "ssid": str(d.get("ssid", "")).strip('"')}


class _QuietServer(ThreadingHTTPServer):
    """Failed handshakes (plain http://, a client that doesn't trust the certificate, port scans)
    are expected noise: one audit-free log line instead of a traceback."""

    def handle_error(self, request, client_address):
        err = sys.exc_info()[1]
        if isinstance(err, (ssl.SSLError, ConnectionError, TimeoutError)):
            log.debug("App connection from %s dropped: %s", client_address[0], err)
            return
        super().handle_error(request, client_address)


class Doors:
    """Opens the app on each configured address, keeps them current, closes what no longer fits.

    - "127.0.0.1" (local): the app on the tablet itself
    - a Tailscale address: from anywhere, through Tailscale
    - "wifi": the tablet's Wi-Fi address, but ONLY on networks you named in WEB_TRUSTED_WIFI
      (your hotspot, home). On a café or hotel network this door stays shut.
    Checked every minute. With HTTPS, the certificate is re-issued when the addresses change.
    """

    def __init__(self, app: "App", port: int, entries: list[str], trusted_wifi: list[str],
                 tls: bool, wifi=wifi_info, extra_hosts: list[str] | None = None, manual_cert=None):
        self.app, self.port, self.entries, self.tls = app, port, entries, tls
        self.trusted = {t.strip() for t in trusted_wifi if t.strip()}
        self.wifi, self.extra_hosts, self.manual_cert = wifi, extra_hosts or [], manual_cert
        self.servers: dict[str, ThreadingHTTPServer] = {}
        self.lock = threading.Lock()
        self.ctx = None
        self.cert_for: tuple = ()
        self.wifi_note = ""
        if tls:
            self.ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
            self.ctx.minimum_version = ssl.TLSVersion.TLSv1_2
            if manual_cert:
                self.ctx.load_cert_chain(*manual_cert)

    def wanted(self) -> list[str]:
        out = []
        for e in self.entries:
            if e != "wifi":
                out.append(e)
                continue
            w = self.wifi()
            if w is None:
                self.wifi_note = "not on Wi-Fi"
            elif not self.tls:
                self.wifi_note = "needs HTTPS (pkg install openssl-tool)"
            elif w["ssid"] not in self.trusted:
                self.wifi_note = f"“{w['ssid'] or 'unknown network'}” isn't a trusted network"
            elif not certs.allowed_address(w["ip"]) or ipaddress.ip_address(w["ip"]).is_loopback:
                self.wifi_note = f"{w['ip']} isn't a private Wi-Fi address"
            else:
                self.wifi_note = f"open on “{w['ssid']}”"
                out.append(w["ip"])
        return list(dict.fromkeys(out))

    def refresh(self) -> list[str]:
        with self.lock:
            want = self.wanted()
            if self.tls and not self.manual_cert and want and tuple(want) != self.cert_for:
                cert, key = certs.leaf([a for a in want if certs.allowed_address(a)] or ["127.0.0.1"])
                self.ctx.load_cert_chain(cert, key)        # new handshakes use the new certificate
                self.cert_for = tuple(want)
            for addr in [a for a in self.servers if a not in want]:
                srv = self.servers.pop(addr)
                srv.shutdown()
                srv.server_close()
                shiba.audit("web_door_closed", address=addr)
            for addr in [a for a in want if a not in self.servers]:
                try:
                    self.servers[addr] = self._open(addr)
                    shiba.audit("web_door_opened", address=addr, https=self.tls)
                except OSError as e:                         # e.g. Tailscale isn't up yet: try again next minute
                    log.info("App door %s not open yet: %s", addr, e)
            hosts = set(self.servers) | set(self.extra_hosts)
            if "127.0.0.1" in self.servers:
                hosts.add("localhost")
            self.app.hosts = hosts
            return list(self.servers)

    def _open(self, addr: str) -> ThreadingHTTPServer:
        server = _QuietServer((addr, self.port), make_handler(self.app))
        server.daemon_threads = True
        if self.tls:
            # The handshake happens in each connection's own thread, so a slow client can't block the door.
            server.socket = self.ctx.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
        threading.Thread(target=server.serve_forever, name=f"web-{addr}", daemon=True).start()
        return server

    def urls(self) -> list[dict]:
        scheme = "https" if self.tls else "http"
        kind = lambda a: "this tablet" if a == "127.0.0.1" else "Tailscale" \
            if ipaddress.ip_address(a) in ipaddress.ip_network("100.64.0.0/10") else "Wi-Fi"
        return [{"url": f"{scheme}://{a}:{self.port}", "kind": kind(a)} for a in self.servers]

    def watch(self, every: int = 60) -> None:
        def loop():
            while True:
                time.sleep(every)
                try:
                    self.refresh()
                except Exception:
                    log.exception("App doors check failed")
        threading.Thread(target=loop, name="web-doors", daemon=True).start()

    def close(self) -> None:
        with self.lock:
            for srv in self.servers.values():
                srv.shutdown()
                srv.server_close()
            self.servers = {}


def start(loop: asyncio.AbstractEventLoop, busy=lambda: False, notify=None, now=lambda: "idle",
          bind: str | None = None, port: int | None = None) -> Doors | None:
    """Open the app's doors, or explain why not. Returns the running Doors, or None."""
    entries = parse_doors(config.WEB_BIND if bind is None else bind)
    port = config.WEB_PORT if port is None else port
    if not entries:
        log.info("App off: WEB_BIND is empty")
        return None
    refused = [(e, door_problem(e, config.WEB_ALLOW_ALL)) for e in entries]
    for e, problem in refused:
        if problem:
            log.warning("App door %s refused: %s", e, problem)
    entries = [e for e, problem in refused if not problem]
    if not entries:
        return None
    if not config.WEB_PASSWORD_HASH.startswith("scrypt$"):
        log.warning("App off: no password yet. Run: python set_password.py")
        return None
    manual = (config.WEB_CERT, config.WEB_KEY) if config.WEB_CERT and config.WEB_KEY else None
    tls = bool(manual) or (config.WEB_TLS != "off" and certs.available())
    if config.WEB_TLS != "off" and not tls:
        log.warning("App without HTTPS: install openssl (pkg install openssl-tool) and restart")
    gate = Gate(config.WEB_PASSWORD_HASH, notify=notify, store=config.DATA_DIR)
    app = App(gate, loop, set(), busy, tls, now)
    doors = Doors(app, port, entries, config.WEB_TRUSTED_WIFI, tls, extra_hosts=config.WEB_HOSTS,
                  manual_cert=manual)
    app.doors = doors
    opened = doors.refresh()
    doors.watch()
    shiba.audit("web_started", doors=", ".join(opened), https=tls)
    for d in doors.urls():
        log.info("App on %s (%s)", d["url"], d["kind"])
    return doors
