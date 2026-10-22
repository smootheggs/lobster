"""Minimal client for the local Ollama server (standard library only).

Ollama runs on this tablet, so requests here NEVER go through the proxy.
"""
import json
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
