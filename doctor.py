"""🦞 Sloth Lobster's check-up: one line per part, ✅ when fine, ⚠️ with what to do when not.

    cd ~/lobster && python doctor.py

It only looks: it changes nothing, starts nothing, and prints no secrets.
"""
import os
import shutil
import socket
import stat
import sys
import time
from pathlib import Path

import config


def _proc_running(script: bytes) -> bool:
    """A python process running exactly this script (not a shell or editor mentioning it)."""
    for pid in os.listdir("/proc") if os.path.isdir("/proc") else []:
        if pid.isdigit() and int(pid) != os.getpid():
            try:
                args = open(f"/proc/{pid}/cmdline", "rb").read().split(b"\0")
            except OSError:
                continue
            if args and b"python" in Path(args[0].decode(errors="replace")).name.encode() \
                    and any(a == script or a.endswith(b"/" + script) for a in args[1:3]):
                return True
    return False


def checks() -> list[tuple[bool, str]]:
    """[(ok, line)]. Every check is wrapped: a broken part shows as ⚠️, never a crash."""
    out: list[tuple[bool, str]] = []

    def add(ok, good, bad):
        out.append((bool(ok), good if ok else bad))

    def safely(name, fn):
        try:
            fn()
        except Exception as e:
            out.append((False, f"{name}: couldn't check ({type(e).__name__}: {e})"))

    out.append((True, f"Version {config.VERSION} · Python {sys.version.split()[0]}"))

    def seal():
        import shiba
        problem = shiba.seal_problem()
        add(not problem, "Seal intact", f"Seal: {problem} → read the changes, then: python seal.py")
    safely("Seal", seal)

    def env():
        p = config.BASE_DIR / ".env"
        if not p.exists():
            return add(False, "", ".env missing → cp .env.example .env and fill it in")
        mode = stat.S_IMODE(p.stat().st_mode)
        add(mode & 0o077 == 0, ".env readable only by you", f".env is shared ({oct(mode)}) → chmod 600 ~/lobster/.env")
        add(bool(config.TELEGRAM_TOKEN) and bool(config.ALLOWED_USER_ID), "Telegram token and your ID are set",
            "Telegram token or ALLOWED_USER_ID missing in .env")
    safely(".env", env)

    add(_proc_running(b"bot.py"), "Sloth Lobster is running",
        "Sloth Lobster isn't running → sh ~/lobster/scripts/start-sloth-lobster.sh")

    def models():
        import json
        import ollama
        if not ollama.alive():
            return add(False, "", "Ollama isn't answering → ollama serve &")
        with ollama._opener.open(f"{config.OLLAMA_URL}/api/tags", timeout=10) as r:
            have = {m["name"] for m in json.loads(r.read()).get("models", [])}
        have |= {n.removesuffix(":latest") for n in have}
        for m in dict.fromkeys([config.MODEL, config.WRITER_MODEL, config.EMBED_MODEL]):
            add(m in have, f"Model {m} is installed", f"Model {m} is missing → ollama pull {m}")
        deep = config.DEEP_WRITER_MODEL
        if deep and deep != config.WRITER_MODEL:
            add(deep in have, f"Deep research writer {deep} is installed (overnight, {config.DEEP_HOURS} h)",
                f"Deep research will write with {config.WRITER_MODEL}: for the bigger writer, ollama pull {deep} (about 4.7 GB)")
        problem = ollama.version_problem(max_age=0)
        add(not problem, "Ollama is up to date", f"{problem}")
    safely("Ollama", models)

    def app():
        import web
        entries = web.parse_doors(config.WEB_BIND)
        if not entries:
            return add(True, "App: off (WEB_BIND is empty)", "")
        add(config.WEB_PASSWORD_HASH.startswith("scrypt$"), "App password is set", "No app password → python set_password.py")
        for e in entries:
            problem = web.door_problem(e, config.WEB_ALLOW_ALL)
            add(not problem, f"App door {e} is allowed", f"App door {e}: {problem}")
        if "wifi" in entries and not shutil.which("termux-wifi-connectioninfo") and not config.WEB_HOTSPOT_IP:
            add(False, "", "Wi-Fi door: set WEB_HOTSPOT_IP (your tablet's fixed hotspot address) in .env")
    safely("App", app)

    def certificates():
        import certs
        if config.WEB_TLS == "off" or not config.WEB_BIND:
            return
        if not certs.available():
            return add(False, "", "No HTTPS: pkg install openssl-tool")
        if certs.CA_CERT.exists():
            days = certs.days_left(certs.CA_CERT)
            add(days is not None and days > 60, f"Your certificate authority is valid for {days} more days",
                f"Your certificate authority expires in {days} days")
        leaves = sorted(certs.TLS_DIR.glob("server-*.crt"), key=lambda p: p.stat().st_mtime)
        if leaves:
            days = certs.days_left(leaves[-1])
            add(days is not None and days > certs.RENEW_DAYS, f"App certificate valid for {days} more days (renews itself)",
                f"App certificate expires in {days} days: it renews within an hour while Sloth Lobster runs")
    safely("Certificates", certificates)

    def vault_check():
        add(config.VAULT_DIR.is_dir() and os.access(config.VAULT_DIR, os.W_OK), f"Vault reachable: {config.VAULT_DIR}",
            f"Vault not reachable at {config.VAULT_DIR} → check VAULT_DIR in .env and termux-setup-storage")
    safely("Vault", vault_check)

    def backups():
        import memory
        last = memory.last_backup()
        if not last:
            return add(False, "", "No backup yet: one is made each night (or run /status to see when)")
        files = sorted(config.BACKUP_DIR.glob("*"), key=lambda p: p.stat().st_mtime) if config.BACKUP_DIR.exists() else []
        age = (time.time() - files[-1].stat().st_mtime) / 3600 if files else None
        add(age is not None and age < 48, f"Last backup {last} ({age:.0f} h ago) in the work folder Autosync uploads",
            f"Last backup {last} is old → is Sloth Lobster running at night?")
    safely("Backups", backups)

    def space():
        for label, path in (("tablet", config.DATA_DIR), ("shared storage", config.VAULT_DIR)):
            if path.exists():
                free = shutil.disk_usage(path).free / 1e9
                add(free > 2, f"Free space ({label}): {free:.1f} GB", f"Low space ({label}): {free:.1f} GB")
    safely("Space", space)

    def offline_wiki():
        import zim
        books = zim.library(config.KIWIX_DIR)
        if not books:
            return add(True, "Offline Wikipedia: none (optional)", "")
        add(True, "Offline Wikipedia: " + ", ".join(z.copy for z in books), "")
        try:
            import compression.zstd  # noqa: F401
            has = True
        except ImportError:
            has = shutil.which("python") is not None and _importable("zstandard")
        add(has, "zstd decompression available", "zstd missing → pip install zstandard")
    safely("Offline Wikipedia", offline_wiki)

    def startup():
        boot = Path.home() / ".termux" / "boot" / "start-sloth-lobster.sh"
        bashrc = Path.home() / ".bashrc"
        auto = bashrc.exists() and "start-sloth-lobster.sh" in bashrc.read_text(errors="replace")
        add(boot.exists() or auto, "Starts by itself (" + ", ".join(
            x for x, y in (("at boot", boot.exists()), ("when Termux opens", auto)) if y) + ")",
            "Doesn't start by itself → see the README's start-up section")
    safely("Start-up", startup)

    def keyword_index():
        import sqlite3
        con = sqlite3.connect(":memory:")
        try:
            con.execute("CREATE VIRTUAL TABLE t USING fts5(x)")
            ok = True
        except sqlite3.OperationalError:
            ok = False
        finally:
            con.close()
        add(ok, "Keyword search available (names and dates are found exactly)",
            "No keyword search in this SQLite: /ask uses meaning only → pkg upgrade")
    safely("Keyword search", keyword_index)

    def time_zones():
        import clock
        add(clock.available(), "Time zones available (the clock answers \"what time is it in …\")",
            "No time-zone data: the clock can't tell other cities' time → pip install tzdata")
    safely("Time zones", time_zones)

    def internet():
        import spider
        spider._online["at"] = 0.0
        add(spider.online(), "Internet reachable", "Offline: research reads the offline copy or waits")
    safely("Internet", internet)
    return out


def _importable(name: str) -> bool:
    try:
        __import__(name)
        return True
    except ImportError:
        return False


def main() -> None:
    socket.setdefaulttimeout(10)
    results = checks()
    for ok, line in results:
        if line:
            print(("✅ " if ok else "⚠️ ") + line)
    bad = sum(1 for ok, line in results if not ok and line)
    print(f"\n🦞 {'All good.' if not bad else f'{bad} thing(s) to look at.'}")


if __name__ == "__main__":
    main()
