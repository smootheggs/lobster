"""🐘 How good is the research? A scorecard from the jobs you already ran. Read-only: it changes
nothing, fetches nothing and calls no model.

    cd ~/lobster && python research_bench.py          # the last 20 finished jobs
    python research_bench.py 50                        # the last 50

Scored by code, the way research agents are judged (completeness, citation accuracy, source
quality, effort):
  answered   sub-questions with an answer (not thin) / all sub-questions
  checked    written sentences that passed the citation check / all written sentences
  sources    distinct sources the brief cites
  time       minutes, collecting through writing
Jobs from before the outline brief (.30) show what can still be counted from their text.
Run it now and again after a week of use, and compare the averages.
"""
import re
import sys

import research


def score(j: dict) -> dict:
    m = j.get("metrics") or {}
    brief = j.get("brief", "")
    sources = m.get("sources", len(re.findall(r"^\[\d+\] https?://", brief, re.M)))
    sections = m.get("sections")
    answered = (sections - m.get("thin", 0)) if sections else None
    written = m.get("sentences", 0) + m.get("dropped", 0)
    return {"id": j["id"], "mode": m.get("mode", j.get("mode", "quick")), "question": j["question"],
            "answered": f"{answered}/{sections}" if sections else "–",
            "answered_share": answered / sections if sections else None,
            "checked": f"{m.get('sentences', 0)}/{written}" if written else "–",
            "checked_share": m.get("sentences", 0) / written if written else None,
            "sources": sources, "rounds": m.get("rounds", 1 if m else None),
            "minutes": round(m["seconds"] / 60, 1) if "seconds" in m else None,
            "writer": m.get("writer", ""), "version": m.get("version", "before .30")}


def main() -> None:
    n = int(sys.argv[1]) if len(sys.argv) > 1 and sys.argv[1].isdigit() else 20
    done = [j for j in research.history(500) if j["status"] == "done"][:n]
    if not done:
        print("🐘 No finished research yet. Run a few with /research or /deep, then come back.")
        return
    rows = [score(j) for j in done]
    print(f"{'#':>4}  {'mode':<5} {'answered':>8} {'checked':>7} {'src':>3} {'rnd':>3} {'min':>5}  question")
    for r in rows:
        print(f"{r['id']:>4}  {r['mode']:<5} {r['answered']:>8} {r['checked']:>7} {r['sources']:>3} "
              f"{r['rounds'] if r['rounds'] is not None else '–':>3} {r['minutes'] if r['minutes'] is not None else '–':>5}  "
              f"{r['question'][:48]}")
    print()
    for mode in ("quick", "deep"):
        mine = [r for r in rows if r["mode"] == mode]
        if not mine:
            continue
        def avg(key):
            vals = [r[key] for r in mine if r[key] is not None]
            return sum(vals) / len(vals) if vals else None
        a, c, s, t = avg("answered_share"), avg("checked_share"), avg("sources"), avg("minutes")
        print(f"🐘 {mode} ({len(mine)} jobs): "
              + " · ".join(x for x in (
                  f"{a:.0%} of sub-questions answered" if a is not None else "",
                  f"{c:.0%} of sentences passed the check" if c is not None else "",
                  f"{s:.1f} sources" if s is not None else "",
                  f"{t:.1f} min" if t is not None else "") if x))
    print("\nSend me this table and we'll see what to tune.")


if __name__ == "__main__":
    main()
