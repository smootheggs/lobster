"""Minimal client for the local Ollama server (standard library only).

Ollama runs on this tablet, so requests here NEVER go through the proxy.
"""
import json
import time
import threading
import urllib.request

import config
import store
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
         keep_alive: str | int = "5m", num_ctx: int = 0, temperature: float = 0.0,
         purpose: str = "chat", threads: int | None = None, think: str | None = None) -> str:
    """Return the model's reply text. With a schema, Ollama forces valid JSON.
    Every call records how long reading the prompt and writing the answer took."""
    options = {"temperature": temperature, "num_ctx": num_ctx or config.MODEL_CTX}
    threads = config.MODEL_THREADS if threads is None else threads
    if threads:
        options["num_thread"] = threads
    payload = {"model": model, "messages": messages, "stream": False, "keep_alive": keep_alive, "options": options}
    if schema:
        payload["format"] = schema
    # Only thinking models accept the switch; others would refuse the request.
    if (config.MODEL_THINK if think is None else think) == "off" and model.startswith(THINKING_MODELS):
        payload["think"] = False
    started = time.monotonic()
    r = _post("/api/chat", payload)
    _record(purpose, model, r, time.monotonic() - started)
    return r["message"]["content"]


LAST = {}     # the most recent call's numbers, for bench.py
THINKING_MODELS = ("qwen3", "deepseek-r1", "magistral", "gpt-oss")


def _record(purpose: str, model: str, r: dict, seconds: float) -> None:
    """Ollama reports how long it spent loading, reading the prompt and writing the answer."""
    ns = lambda k: round((r.get(k) or 0) / 1e9, 2)
    row = {"step": f"model_{purpose}", "seconds": round(seconds, 2), "model": model,
           "load_s": ns("load_duration"), "read_s": ns("prompt_eval_duration"), "write_s": ns("eval_duration"),
           "in_tokens": r.get("prompt_eval_count") or 0, "out_tokens": r.get("eval_count") or 0}
    LAST.clear()
    LAST.update(row)
    if store._timings_file is not None:
        try:
            store.append_jsonl(store._timings_file, row)
        except OSError:
            pass


def speed(purpose: str = "chat", last: int = 20) -> dict | None:
    """Average seconds for the last few calls: total, reading, writing."""
    if store._timings_file is None:
        return None
    rows = [r for r in store.read_jsonl_tail(store._timings_file) if r.get("step") == f"model_{purpose}"][-last:]
    if not rows:
        return None
    avg = lambda k: round(sum(float(r.get(k) or 0) for r in rows) / len(rows), 1)
    return {"seconds": avg("seconds"), "read_s": avg("read_s"), "write_s": avg("write_s"),
            "load_s": avg("load_s"), "calls": len(rows)}


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


_installed = {"at": 0.0, "names": set()}


def installed(model: str, max_age: float = 600) -> bool:
    """Is this model downloaded? (Checked at most every 10 minutes.)"""
    if time.time() - _installed["at"] > max_age:
        try:
            with _opener.open(f"{OLLAMA_URL}/api/tags", timeout=10) as r:
                names = {m["name"] for m in json.loads(r.read()).get("models", [])}
            _installed.update(at=time.time(), names=names | {n.removesuffix(":latest") for n in names})
        except Exception:
            return False
    return model in _installed["names"]


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
