"""Fail when repository Markdown links point to a missing local artifact."""
from __future__ import annotations

import argparse
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LINK = re.compile(r"(?<!!)\[[^]]*\]\(([^)]+)\)")


def check(root: Path = ROOT) -> list[str]:
    root = Path(root)
    files = [root / "README.md", root / "OPEN-SOURCE.md", root / "SECURITY.md"]
    docs = root / "docs"
    if docs.exists():
        files.extend(sorted(docs.rglob("*.md")))
    errors = []
    for path in files:
        if not path.is_file():
            continue
        for target in LINK.findall(path.read_text(encoding="utf-8")):
            target = target.strip().strip("<>")
            if not target or target.startswith(("https://", "http://", "mailto:", "#")):
                continue
            local = target.split("#", 1)[0]
            if not local:
                continue
            destination = (path.parent / local).resolve()
            if not destination.exists() or root.resolve() not in destination.parents and destination != root.resolve():
                errors.append(f"{path.relative_to(root)} -> {target}")
    return errors


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=ROOT)
    args = parser.parse_args(argv)
    errors = check(args.root)
    if errors:
        print("Broken local documentation links:")
        print("\n".join(f"- {error}" for error in errors))
        return 1
    print("Documentation links are valid")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
