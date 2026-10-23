"""Used by update.sh: compare, apply and roll back Sloth Lobster's code files.

It only ever touches files that are part of the package (code, the app, scripts). Your data
folder, .env, the .git folder and anything else you keep in ~/lobster are never read or
changed. The previous version is kept in ~/.lobster-prev, so an update can be undone.

    python swap.py changes <new> <live>          exit 3 if sealed files change
    python swap.py apply   <new> <live> <prev>
    python swap.py rollback <live> <prev>
"""
import filecmp
import json
import re
import shutil
import sys
from pathlib import Path

SEALED = ("identity.txt", "actions.py", "shiba.py", "web.py")
NEVER = {"data", ".env", ".git", "__pycache__", ".pytest_cache"}


def package_files(root: Path) -> list[str]:
    """Relative paths of the package's files under root (never data, .env or .git)."""
    out = []
    for p in sorted(root.rglob("*")):
        rel = p.relative_to(root)
        if p.is_file() and not (set(rel.parts) & NEVER) and p.suffix != ".pyc":
            out.append(rel.as_posix())
    return out


def version(root: Path) -> str:
    m = re.search(r'^VERSION = "([^"]+)"', (root / "config.py").read_text(encoding="utf-8"), re.M) \
        if (root / "config.py").exists() else None
    return m.group(1) if m else "unknown"


def notes(root: Path, ver: str) -> str:
    """The release notes for this version, from CHANGES.md."""
    p = root / "CHANGES.md"
    if not p.exists():
        return ""
    text = p.read_text(encoding="utf-8")
    m = re.search(rf"^## {re.escape(ver)}\b.*?(?=^## |\Z)", text, re.M | re.S)
    return m.group(0).strip() if m else ""


def changes(new: Path, live: Path) -> int:
    changed = [f for f in package_files(new)
               if not (live / f).exists() or not filecmp.cmp(new / f, live / f, shallow=False)]
    sealed = [f for f in changed if f in SEALED]
    text = notes(new, version(new))
    if text:
        print("\n" + "\n".join("   " + line for line in text.splitlines()) + "\n")
    print(f"   {len(changed)} file{'s' if len(changed) != 1 else ''} change"
          + (f": {', '.join(changed[:12])}{' …' if len(changed) > 12 else ''}" if changed else ""))
    if sealed:
        print("🐕 Sealed files change: " + ", ".join(sealed))
        for f in sealed:
            print(f"   read: diff ~/lobster/{f} ~/.lobster-next/lobster/{f}")
        return 3
    return 0


def apply(new: Path, live: Path, prev: Path) -> int:
    files = package_files(new)
    if prev.exists():
        shutil.rmtree(prev)                          # the copy of the version before last: ours, not your data
    prev.mkdir(parents=True)
    added = []
    for f in files:
        if (live / f).exists():
            (prev / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(live / f, prev / f)
        else:
            added.append(f)
    (prev / ".added.json").write_text(json.dumps(added), encoding="utf-8")
    for f in files:
        (live / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(new / f, live / f)
    print(f"🦞 Installed {version(live)}. The previous version is kept: sh ~/lobster/scripts/update.sh --rollback")
    return 0


def rollback(live: Path, prev: Path) -> int:
    if not prev.exists() or not (prev / ".added.json").exists():
        print("🦞 There's no previous version to go back to.")
        return 1
    added = json.loads((prev / ".added.json").read_text(encoding="utf-8"))
    for f in package_files(prev):
        (live / f).parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(prev / f, live / f)
    parked = prev / "added-by-the-undone-update"
    for f in added:                                  # files the undone update brought: moved aside, never deleted
        if (live / f).exists():
            (parked / f).parent.mkdir(parents=True, exist_ok=True)
            shutil.move(str(live / f), str(parked / f))
    print(f"🦞 Back on {version(live)}.")
    return 0


def main(argv: list[str]) -> int:
    if len(argv) >= 3 and argv[0] == "changes":
        return changes(Path(argv[1]), Path(argv[2]))
    if len(argv) >= 4 and argv[0] == "apply":
        return apply(Path(argv[1]), Path(argv[2]), Path(argv[3]))
    if len(argv) >= 3 and argv[0] == "rollback":
        return rollback(Path(argv[1]), Path(argv[2]))
    print(__doc__)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
