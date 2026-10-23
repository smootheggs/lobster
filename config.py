"""Settings for Sloth Lobster. Values come from a .env file next to this one."""
import os
import stat
from pathlib import Path

BASE_DIR = Path(__file__).parent
ENV_FILE = BASE_DIR / ".env"


def _load_env(path: Path) -> None:
    """Tiny .env reader: KEY=value lines, # for comments."""
    if not path.exists():
        return
    # Secrets file must be readable by you only (blocks the "leak the config" attack).
    mode = path.stat().st_mode
    if mode & (stat.S_IRWXG | stat.S_IRWXO):
        path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # chmod 600
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip())


_load_env(ENV_FILE)

# --- Telegram ---
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN", "")          # from @BotFather
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))  # your numeric Telegram ID
PROXY = os.getenv("PROXY", "")                             # e.g. http://127.0.0.1:7890 in China

# --- Local models (Ollama) ---
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
MODEL = os.getenv("MODEL", "qwen2.5:3b")                  # deciding what you want + chat replies
WRITER_MODEL = os.getenv("WRITER_MODEL", "") or MODEL      # research plans and briefs (can be a bigger model)
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
CHAT_KEEP_ALIVE = os.getenv("CHAT_KEEP_ALIVE", "24h")     # keep the model loaded: no cold start after a pause
MODEL_THREADS = int(os.getenv("MODEL_THREADS", "0") or 0)  # 0: Ollama decides; bench.py finds the fastest
MODEL_CTX = 4096          # one window size for every call: changing it makes Ollama reload the model
MODEL_THINK = os.getenv("MODEL_THINK", "").lower()          # "off" turns off thinking (Qwen3); else unset

# --- Offline Wikipedia: Kiwix .zim files (wikipedia_<lang>_..._YYYY-MM.zim) read straight from disk ---
KIWIX_DIR = Path(os.getenv("KIWIX_DIR", "/storage/emulated/0/Kiwix"))

# --- Storage ---
# Sloth Lobster's private working state stays on the tablet's internal storage.
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
CACHE_DIR = Path(os.getenv("CACHE_DIR", DATA_DIR / "cache"))   # the Spider's raw downloads
DB_PATH = DATA_DIR / "memory.db"                             # the Elephant's index of facts and cards
MEMORY_MD = DATA_DIR / "MEMORY.md"                          # facts you approve; you can edit it
ARCHIVE_DIR = os.getenv("ARCHIVE_DIR", "")                  # optional: USB SSD folder for old raw cache

# Your vault: the Elephant writes cards, source pages and maps into VAULT_DIR/<VAULT_INBOX>.
VAULT_DIR = Path(os.getenv("VAULT_DIR", DATA_DIR / "vault"))
VAULT_INBOX = os.getenv("VAULT_INBOX", "Lobster Inbox")
# Working files (documents, backups) live apart from the knowledge base; sync this folder to Drive too.
WORK_DIR = Path(os.getenv("WORK_DIR", DATA_DIR / "work"))
LIBRARY_DIR = Path(os.getenv("LIBRARY_DIR", WORK_DIR / "Library"))   # /save documents and their receipts
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", WORK_DIR / "Backups"))      # nightly memory backups (newest 14)
# Your loci: the subject areas a card can belong to (edit freely; room to grow).
LOCI = [l.strip() for l in os.getenv(
    "LOCI", "politics, economics, world history, markets and trading, language and culture").split(",") if l.strip()]

# --- Web identity: sites like Wikipedia ask bots for a way to contact their owner ---
CONTACT = os.getenv("CONTACT", "personal research bot")      # e.g. an email or your GitHub URL

# --- The name you see ---
APP_NAME = os.getenv("APP_NAME", "Sloth Lobster")

# --- The app (off until you set WEB_BIND and run set_password.py) ---
# Doors, comma-separated: "local" (the tablet itself), your Tailscale address, and "wifi"
# (the tablet's Wi-Fi address, only on networks named in WEB_TRUSTED_WIFI). Never 0.0.0.0.
WEB_BIND = os.getenv("WEB_BIND", "")
WEB_TRUSTED_WIFI = [w.strip() for w in os.getenv("WEB_TRUSTED_WIFI", "").split(",") if w.strip()]
# Without Termux:API (Google Play Termux) the network name can't be read: give the tablet a fixed
# address on your hotspot (Android Wi-Fi settings > the hotspot > IP settings: Static) and put it here.
WEB_HOTSPOT_IP = os.getenv("WEB_HOTSPOT_IP", "").strip()
WEB_TLS = os.getenv("WEB_TLS", "auto").lower()       # auto: HTTPS with Sloth Lobster's own certificate
WEB_PORT = int(os.getenv("WEB_PORT", "8787"))
WEB_ALLOW_ALL = os.getenv("WEB_ALLOW_ALL", "0") == "1"   # only if you really mean every network
WEB_HOSTS = [h.strip().lower() for h in os.getenv("WEB_HOSTS", "").split(",") if h.strip()]  # extra names, e.g. Tailscale
WEB_PASSWORD_HASH = os.getenv("WEB_PASSWORD_HASH", "")   # written by set_password.py; never the password itself
WEB_CERT = os.getenv("WEB_CERT", "")                 # optional HTTPS: certificate and key files
WEB_KEY = os.getenv("WEB_KEY", "")

# --- Schedules ---
DIGEST_ON_CHARGE = os.getenv("DIGEST_ON_CHARGE", "1") == "1"

VERSION = "2026-10-22.28"

import store  # noqa: E402  (after the paths above are known)
store.set_timings_file(DATA_DIR / "timings.jsonl")

IDENTITY = (BASE_DIR / "identity.txt").read_text(encoding="utf-8")
