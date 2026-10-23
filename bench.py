"""🦞 Model bake-off: which model and thread count make Sloth Lobster fastest without
getting dumber? Runs on the tablet against your real Ollama. It only asks questions:
it never writes to your vault, notes or memory, and records nothing in your timings.

    python bench.py                                   # the default line-up, threads 0 (auto), 4, 6
    python bench.py --models qwen2.5:3b,qwen3:1.7b --threads 4,6,8
    python bench.py --quick                           # fewer test messages, one thread setting

Models you haven't downloaded are skipped with the `ollama pull` line to get them.
"""
import argparse
import json
import sys
import time

import config
import store

store._timings_file = None               # the bake-off must not mix into your real timings

import brain      # noqa: E402
import elephant   # noqa: E402
import ollama     # noqa: E402

LINEUP = ["qwen2.5:3b", "qwen3:1.7b", "qwen3:4b", "llama3.2:3b"]

# What you say, and the action Sloth Lobster should pick.
CASES = [
    ("Can you find out how the Hanseatic League declined?", "research"),
    ("Look into the history of the Bundesbank for me", "research"),
    ("What's going on with copper prices this month? Research it.", "research"),
    ("Remember that my passport renewal is due in March", "save_note"),
    ("Save this: the ferry leaves Hamburg at 9:40 on Saturdays", "save_note"),
    ("Note that I prefer German sources for EU topics", "save_note"),
    ("What did I save about the Treaty of Westphalia?", "ask"),
    ("Do my notes say anything about Lübeck?", "ask"),
    ("Show me my recent notes", "list_notes"),
    ("What notes did I write lately?", "list_notes"),
    ("How are you today?", "reply"),
    ("Explain in one sentence what a tariff is", "reply"),
]

FACTS = [{"text": t} for t in (
    "The Hanseatic League was a medieval commercial and defensive network of merchant guilds and market towns.",
    "Lübeck became the leading city of the League in the 14th century.",
    "The Treaty of Stralsund in 1370 marked the peak of the League's power.",
    "The League controlled trade routes across the Baltic and North Seas.",
    "Its main trading posts, called Kontore, included London, Bruges, Bergen and Novgorod.",
    "The rise of territorial states weakened the League from the 15th century onward.",
    "Dutch competition in Baltic trade grew strongly during the 16th century.",
    "The last formal diet of the League met in 1669.",
    "Hamburg, Bremen and Lübeck kept the name Hanseatic cities afterwards.",
    "Herring and grain were among the most important goods traded.",
)]


def installed() -> set[str]:
    with ollama._opener.open(f"{config.OLLAMA_URL}/api/tags", timeout=10) as r:
        names = {m["name"] for m in json.loads(r.read()).get("models", [])}
    return names | {n.removesuffix(":latest") for n in names}


def router_case(model: str, message: str, think: str | None) -> tuple[str | None, float, float, float]:
    messages = [{"role": "system", "content": brain._system_prompt([])}, {"role": "user", "content": message}]
    started = time.monotonic()
    try:
        content = ollama.chat(model, messages, schema=brain.SCHEMA, keep_alive="10m",
                              purpose="bench", think=think)
        action = json.loads(content).get("action")
    except Exception:
        action = None
    return action, time.monotonic() - started, ollama.LAST.get("read_s", 0), ollama.LAST.get("write_s", 0)


def run(model: str, threads: int, cases: list[tuple[str, str]]) -> dict:
    think = "off" if model.startswith(("qwen3", "deepseek-r1")) else None
    config.MODEL_THREADS = threads                     # read by every ollama.chat call
    config.MODEL_THINK = think or ""
    ollama.unload(model)
    t0 = time.monotonic()
    router_case(model, "Hello", think)                 # first call: includes loading the model
    load = time.monotonic() - t0
    right, secs, reads, writes = 0, [], [], []
    for message, expected in cases:
        action, s, r, w = router_case(model, message, think)
        right += action == expected
        secs.append(s), reads.append(r), writes.append(w)
    elephant.WRITER_MODEL = model
    t0 = time.monotonic()
    _, points = elephant.choose_points("Why did the Hanseatic League decline?", FACTS)
    brief = time.monotonic() - t0
    return {"model": model, "threads": threads or "auto", "load": load, "right": right, "of": len(cases),
            "reply": sum(secs) / len(secs), "read": sum(reads) / len(reads), "write": sum(writes) / len(writes),
            "brief": brief, "points": len(points)}


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--models", default=",".join(LINEUP))
    ap.add_argument("--threads", default="0,4,6")
    ap.add_argument("--quick", action="store_true")
    a = ap.parse_args()
    if not ollama.alive():
        sys.exit("Ollama isn't running. Start it first: ollama serve &")
    have = installed()
    models = [m.strip() for m in a.models.split(",") if m.strip()]
    threads = [int(t) for t in a.threads.split(",")] if not a.quick else [0]
    cases = CASES[::2] if a.quick else CASES
    for m in [m for m in models if m not in have]:
        print(f"⏭️  {m} isn't downloaded. To include it: ollama pull {m}")
    models = [m for m in models if m in have]
    rows = []
    for m in models:
        for t in threads:
            print(f"🦞 Testing {m} with threads={t or 'auto'}…", flush=True)
            rows.append(run(m, t, cases))
            r = rows[-1]
            print(f"   {r['right']}/{r['of']} right · reply {r['reply']:.1f} s (read {r['read']:.1f} · write "
                  f"{r['write']:.1f}) · brief {r['brief']:.1f} s ({r['points']} points) · load {r['load']:.1f} s")
    if not rows:
        return
    print("\nmodel            threads  right   reply s  read  write  brief s  load s")
    for r in sorted(rows, key=lambda r: r["reply"]):
        print(f"{r['model']:<16} {str(r['threads']):<7}  {r['right']:>2}/{r['of']:<3} {r['reply']:>7.1f} "
              f"{r['read']:>5.1f} {r['write']:>6.1f} {r['brief']:>8.1f} {r['load']:>7.1f}")
    best_right = max(r["right"] for r in rows)
    good = [r for r in rows if r["right"] >= best_right - 1]          # at most one more mistake than the best
    fast = min(good, key=lambda r: r["reply"])
    writer = min([r for r in rows if r["points"] >= 5] or rows, key=lambda r: r["brief"])
    print(f"\n🦞 Fastest without getting dumber: {fast['model']} with threads={fast['threads']} "
          f"({fast['right']}/{fast['of']} right, {fast['reply']:.1f} s per reply).")
    print("   To use it, put these lines in .env and restart:")
    print(f"   MODEL={fast['model']}")
    if fast["threads"] != "auto":
        print(f"   MODEL_THREADS={fast['threads']}")
    if fast["model"].startswith("qwen3"):
        print("   MODEL_THINK=off")
    if writer["model"] != fast["model"]:
        print(f"   WRITER_MODEL={writer['model']}      # quickest brief with at least 5 good points")
    print("\nSend me this table and I'll check the choice with you before you switch.")


if __name__ == "__main__":
    main()
