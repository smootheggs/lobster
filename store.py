"""Small, safe file helpers shared by every part of Sloth Lobster.

  append_jsonl -> add one record to an append-only log
  read_jsonl   -> read a log back; a half-written last line (say, after the
                  tablet died mid-write) is skipped instead of breaking everything
  write_atomic -> replace a file in one step, so nothing ever sees it half-written
  timed        -> measure a step (research, digest, brief...) so speed is known, not guessed
"""
import json
import time
from contextlib import contextmanager
from pathlib import Path


def append_jsonl(path: Path, record: dict, stamp: str | None = "%Y-%m-%d %H:%M") -> dict:
    path.parent.mkdir(parents=True, exist_ok=True)
    if stamp:
        record = {**record, "ts": time.strftime(stamp)}
    with path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    return record


def read_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def read_jsonl_tail(path: Path, max_bytes: int = 256 * 1024) -> list[dict]:
    """Only the newest lines of a log that keeps growing (e.g. timings): reads at most the
    last 256 KB, so the app's status stays quick after months of use."""
    try:
        with path.open("rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - max_bytes))
            raw = f.read()
    except OSError:
        return []
    lines = raw.decode("utf-8", "replace").splitlines()
    if size > max_bytes:
        lines = lines[1:]                          # the first line may be cut in half
    out = []
    for line in lines:
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def write_atomic(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if isinstance(data, bytes):
        tmp.write_bytes(data)
    else:
        tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)


# --- Timings: every slow step records how long it took --------------------------

_timings_file: Path | None = None


def set_timings_file(path: Path) -> None:
    global _timings_file
    _timings_file = path


@contextmanager
def timed(step: str):
    start = time.monotonic()
    try:
        yield
    finally:
        if _timings_file is not None:
            try:
                append_jsonl(_timings_file, {"step": step, "seconds": round(time.monotonic() - start, 2)})
            except OSError:
                pass


def timing_summary(last: int = 20) -> dict[str, float]:
    """Average seconds per step over its last few runs."""
    if _timings_file is None:
        return {}
    runs: dict[str, list[float]] = {}
    for r in read_jsonl_tail(_timings_file):
        if isinstance(r.get("seconds"), (int, float)):
            runs.setdefault(str(r.get("step")), []).append(r["seconds"])
    return {k: round(sum(v[-last:]) / len(v[-last:]), 1) for k, v in runs.items()}
