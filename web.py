"""Lobster's local web app: a second door into the same house.

The app on your phones talks to this small server, which runs inside the bot's
process and calls the same conversation engine as Telegram, so every message
gets exactly the same Shiba checks and one-time approval cards.

The door's locks (all here, all plain code, sealed with Shiba's rules):
  - off unless WEB_BIND and a password hash are set; never on 0.0.0.0 by accident
  - only answers requests addressed to the tablet's own name (stops DNS tricks)
  - the password is kept only as a scrypt hash, set by set_password.py
  - 5 wrong passwords lock the door for 15 minutes, with an audit line and an alert
  - sessions: random token, HttpOnly + SameSite=Strict cookie, 12 hours
  - every change needs the session's anti-forgery (CSRF) token and a JSON body
  - strict security headers on every response; files can't escape web/ or the Library
"""
import asyncio
import hashlib
import hmac
import ipaddress
import json
import logging
import secrets
import ssl
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, unquote, urlparse

import actions
import config
import conversation
import elephant
import memory
import ollama
import research
import shiba
from store import read_jsonl, timing_summary

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
    """Logins, sessions and the lockout. Thread-safe; sessions end when Lobster restarts."""

    def __init__(self, password_hash: str, clock=time.time, notify=None):
        self.password_hash = password_hash
        self.clock = clock
        self.notify = notify or (lambda text: None)
        self.lock = threading.Lock()
        self.sessions: dict[str, dict] = {}
        self.fails = 0
        self.locked_until = 0.0

    def login(self, password: str) -> tuple[dict | None, str]:
        with self.lock:
            now = self.clock()
            if now < self.locked_until:
                shiba.audit("web_login", ok=False, reason="locked")
                return None, f"Too many wrong passwords. Try again in {int(self.locked_until - now) // 60 + 1} minutes."
            ok = bool(self.password_hash) and verify_password(password, self.password_hash)
            shiba.audit("web_login", ok=ok)
            if not ok:
                self.fails += 1
                if self.fails >= MAX_FAILS:
                    self.fails, self.locked_until = 0, now + LOCK_SECONDS
                    shiba.audit("web_lockout", minutes=LOCK_SECONDS // 60)
                    self.notify(f"🐕 Shiba: {MAX_FAILS} wrong passwords on the web app. "
                                f"The login is locked for {LOCK_SECONDS // 60} minutes.")
                return None, "Wrong password."
            self.fails = 0
            session = {"token": secrets.token_urlsafe(32), "csrf": secrets.token_urlsafe(32),
                       "expires": now + SESSION_SECONDS}
            self.sessions = {t: s for t, s in self.sessions.items() if s["expires"] > now}
            self.sessions[session["token"]] = session
            return session, "ok"

    def session(self, token: str | None) -> dict | None:
        if not token:
            return None
        with self.lock:
            s = self.sessions.get(token)
            if s and s["expires"] > self.clock():
                return s
            self.sessions.pop(token, None)
            return None

    def logout(self, token: str) -> None:
        with self.lock:
            self.sessions.pop(token, None)


# --- The app: routes -----------------------------------------------------------------------

class App:
    """The app's API. Every route calls the same functions Telegram does (conversation.py)."""

    def __init__(self, gate: Gate, loop: asyncio.AbstractEventLoop, hosts: set[str],
                 busy=lambda: False, secure: bool = False, now=lambda: "idle"):
        self.gate, self.loop, self.hosts, self.busy, self.secure, self.now = gate, loop, hosts, busy, secure, now

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
                "version": config.VERSION}

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
        return {"reply": conversation.note(self._text(body, "text") or "")}

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
            return {"ok": False, "reply": "⏸️ Lobster is paused. Send /resume first."}
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

    GETS = {"/api/me": me, "/api/status": status, "/api/research": research_list, "/api/cards": cards_list,
            "/api/library": library, "/api/memory": memory_search, "/api/audit": audit_list}
    POSTS = {"/api/chat": chat, "/api/approve": approve, "/api/cancel": cancel, "/api/research": research_new,
             "/api/ask": ask, "/api/note": note, "/api/save": save, "/api/stop": stop, "/api/new": new,
             "/api/review": review, "/api/pause": pause, "/api/resume": resume, "/api/logout": logout}


def make_handler(app: App):
    class Handler(BaseHTTPRequestHandler):
        server_version = "Lobster"
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
                self._error(500, "Something went wrong. Lobster is still running; details in the audit.")
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
                session, why = app.gate.login(str(body.get("password", ""))[:200])
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
            extra = ({"Set-Cookie": f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"}
                     if path == "/api/logout" else None)
            return self._json(200, out, extra)

        do_HEAD = do_GET

    return Handler


def start(loop: asyncio.AbstractEventLoop, busy=lambda: False, notify=None, now=lambda: "idle",
          bind: str | None = None, port: int | None = None) -> ThreadingHTTPServer | None:
    """Open the door, or explain why not. Returns the running server, or None."""
    bind = config.WEB_BIND if bind is None else bind
    port = config.WEB_PORT if port is None else port
    problem = bind_problem(bind, config.WEB_ALLOW_ALL)
    if problem:
        log.info("Web app off: %s", problem)
        return None
    if not config.WEB_PASSWORD_HASH.startswith("scrypt$"):
        log.warning("Web app off: no password yet. Run: python set_password.py")
        return None
    gate = Gate(config.WEB_PASSWORD_HASH, notify=notify)
    secure = bool(config.WEB_CERT and config.WEB_KEY)
    app = App(gate, loop, allowed_hosts(bind, config.WEB_HOSTS), busy, secure, now)
    server = ThreadingHTTPServer((bind, port), make_handler(app))
    server.daemon_threads = True
    if secure:
        ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
        ctx.minimum_version = ssl.TLSVersion.TLSv1_2
        ctx.load_cert_chain(config.WEB_CERT, config.WEB_KEY)
        # The handshake happens in each connection's own thread, so a slow client can't block the door.
        server.socket = ctx.wrap_socket(server.socket, server_side=True, do_handshake_on_connect=False)
    threading.Thread(target=server.serve_forever, name="lobster-web", daemon=True).start()
    shiba.audit("web_started", bind=bind, port=port, https=secure)
    log.info("Web app on %s://%s:%s", "https" if secure else "http", bind, port)
    return server
