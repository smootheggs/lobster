"""Settings for the Lobster. Values come from a .env file next to this one."""
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
MODEL = os.getenv("MODEL", "qwen2.5:3b")                  # chat + fact extraction
EMBED_MODEL = os.getenv("EMBED_MODEL", "nomic-embed-text")
CHAT_KEEP_ALIVE = os.getenv("CHAT_KEEP_ALIVE", "5m")      # how long the model stays in RAM after a chat

# --- Storage (internal storage; keep the database off removable drives) ---
DATA_DIR = Path(os.getenv("DATA_DIR", BASE_DIR / "data"))
CACHE_DIR = Path(os.getenv("CACHE_DIR", DATA_DIR / "cache"))   # Spider's raw downloads
DB_PATH = DATA_DIR / "memory.db"
MEMORY_MD = DATA_DIR / "MEMORY.md"                          # facts you approve; you can edit it
SOURCES_FILE = DATA_DIR / "sources.txt"                     # URLs the Spider may fetch
ARCHIVE_DIR = os.getenv("ARCHIVE_DIR", "")                  # optional: USB SSD folder for old raw cache
BACKUP_DIR = Path(os.getenv("BACKUP_DIR", DATA_DIR / "backups"))  # point it into Slothvault to sync to Drive
LIBRARY_DIR = Path(os.getenv("LIBRARY_DIR", DATA_DIR / "library"))  # /save documents; point it into Slothvault to sync to Drive

# --- Web identity: sites like Wikipedia ask bots for a way to contact their owner ---
CONTACT = os.getenv("CONTACT", "personal research bot")      # e.g. an email or your GitHub URL

# --- Schedules ---
SPIDER_EVERY_HOURS = float(os.getenv("SPIDER_EVERY_HOURS", "6"))
DIGEST_ON_CHARGE = os.getenv("DIGEST_ON_CHARGE", "1") == "1"

VERSION = "2026-10-22.12"

IDENTITY = (BASE_DIR / "identity.txt").read_text(encoding="utf-8")
