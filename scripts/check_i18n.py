"""Verify that the frontend translation catalogue and its literal t() calls agree."""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any

CALL = re.compile(r"\bt\(\s*(['\"])([A-Za-z0-9_.-]+)\1")


@dataclass(frozen=True)
class CheckResult:
    missing: tuple[str, ...]
    unused: tuple[str, ...]

    @property
    def ok(self) -> bool:
        return not self.missing and not self.unused


def _flatten(value: Any, prefix: str = "") -> dict[str, str]:
    if isinstance(value, dict):
        out: dict[str, str] = {}
        for key, child in value.items():
            name = f"{prefix}.{key}" if prefix else str(key)
            out.update(_flatten(child, name))
        return out
    if not isinstance(value, str):
        raise ValueError(f"{prefix}: translation values must be strings")
    return {prefix: value}


def check(root: Path) -> CheckResult:
    root = Path(root)
    catalogue_path = root / "web" / "i18n" / "en.json"
    web_dir = root / "web"
    try:
        catalogue = _flatten(json.loads(catalogue_path.read_text(encoding="utf-8")))
    except (OSError, ValueError, json.JSONDecodeError) as exc:
        raise ValueError(f"cannot read {catalogue_path}: {exc}") from exc
    try:
        sources = sorted(web_dir.rglob("*.js"))
        calls = {
            match.group(2)
            for path in sources
            for match in CALL.finditer(path.read_text(encoding="utf-8"))
        }
    except OSError as exc:
        raise ValueError(f"cannot read JavaScript under {web_dir}: {exc}") from exc
    keys = set(catalogue)
    return CheckResult(tuple(sorted(calls - keys)), tuple(sorted(keys - calls)))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1],
                        help="repository root (default: the parent of scripts/)")
    args = parser.parse_args(argv)
    try:
        result = check(args.root)
    except ValueError as exc:
        print(f"i18n check failed: {exc}", file=sys.stderr)
        return 1
    if result.ok:
        print("i18n check clean")
        return 0
    for key in result.missing:
        print(f"missing translation key: {key}", file=sys.stderr)
    for key in result.unused:
        print(f"unused translation key: {key}", file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
