"""Minimal client for the local Ollama server (standard library only).

Ollama runs on this tablet, so requests here NEVER go through the proxy.
"""
import json
import time
import threading
import urllib.request

from config import OLLAMA_URL

# An opener with an empty proxy map ignores http_proxy/https_proxy env vars.
_opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

# One model on a tablet: every chat and embed call takes its turn here, so a
# chat message and the Elephant's digest never load the model twice at once.
# The digest asks one page-chunk at a time, so a chat waits at most one chunk.
model_turn = threading.Lock()


def _post(path: str, payload: dict, timeout: int = 300) -> dict:
    req = urllib.request.Request(
        f"{OLLAMA_URL}{path}",
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json"},
    )
    with model_turn, _opener.open(req, timeout=timeout) as r:
        return json.loads(r.read())


def chat(model: str, messages: list[dict], schema: dict | None = None,
         keep_alive: str | int = "5m", num_ctx: int = 4096, temperature: float = 0.0) -> str:
    """Return the model's reply text. With a schema, Ollama forces valid JSON."""
    payload = {
        "model": model,
        "messages": messages,
        "stream": False,
        "keep_alive": keep_alive,
        "options": {"temperature": temperature, "num_ctx": num_ctx},
    }
    if schema:
        payload["format"] = schema
    return _post("/api/chat", payload)["message"]["content"]


def embed(model: str, texts: list[str], keep_alive: str | int = "5m") -> list[list[float]]:
    if not texts:
        return []
    return _post("/api/embed", {"model": model, "input": texts, "keep_alive": keep_alive})["embeddings"]


def unload(model: str) -> None:
    """Free the model's RAM right away (keep_alive=0)."""
    try:
        _post("/api/generate", {"model": model, "keep_alive": 0}, timeout=30)
    except Exception:
        pass


def alive() -> bool:
    try:
        with _opener.open(f"{OLLAMA_URL}/api/tags", timeout=5):
            return True
    except Exception:
        return False


SAFE_VERSION = (0, 1, 29)     # fixes the DNS-rebinding flaw CVE-2024-28224


_checked = {"at": -1e9, "problem": None}


def version_problem(max_age: float = 600) -> str | None:
    """A warning if the local Ollama is older than the version that fixed DNS rebinding.
    Checked at most every 10 minutes."""
    if time.time() - _checked["at"] < max_age:
        return _checked["problem"]
    _checked.update(at=time.time(), problem=_version_problem())
    return _checked["problem"]


def _version_problem() -> str | None:
    try:
        with _opener.open(f"{OLLAMA_URL}/api/version", timeout=5) as r:
            v = json.loads(r.read()).get("version", "")
    except Exception:
        return None                                   # down or unknown: /status reports that separately
    try:
        parts = tuple(int(x) for x in v.split("-")[0].split(".")[:3])
    except ValueError:
        return None
    if parts < SAFE_VERSION:
        return f"Ollama {v} is older than {'.'.join(map(str, SAFE_VERSION))}; update it (pkg upgrade ollama)"
    return None
