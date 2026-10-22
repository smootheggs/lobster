"""Small, safe file helpers shared by every part of Lobster.

  append_jsonl -> add one record to an append-only log
  read_jsonl   -> read a log back; a half-written last line (say, after the
                  tablet died mid-write) is skipped instead of breaking everything
  write_atomic -> replace a file in one step, so nothing ever sees it half-written
"""
import json
import time
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


def write_atomic(path: Path, data: str | bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if isinstance(data, bytes):
        tmp.write_bytes(data)
    else:
        tmp.write_text(data, encoding="utf-8")
    tmp.replace(path)
