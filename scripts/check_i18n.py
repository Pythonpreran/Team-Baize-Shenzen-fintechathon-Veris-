"""Verify that the frontend translation catalogue and its literal t() calls agree.

English (`web/i18n/en.json`) is the source catalogue: every literal t() key must exist in
it and every key in it must be used.

Simplified Chinese (`web/i18n/zh-CN.json`, T28) is checked against a declared SCOPE rather
than for a full match. It covers the owner's screens only; adviser, insurer and reviewer
screens stay in English and the app tells a zh-CN reader so. The check therefore fails on:
  * an owner-facing English key with no Chinese translation (the reader would see English
    mid-sentence on a screen that claims to be translated);
  * a Chinese key in an English-only scope (a half-translated professional screen, which
    the app's notice would then misdescribe);
  * a Chinese key that no longer exists in English (stale);
  * a translation whose {placeholders} differ from the English (it would print a literal
    `{name}`, or drop a value the English shows).
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

CALL = re.compile(r"\bt\(\s*(['\"])([A-Za-z0-9_.-]+)\1")
PLACEHOLDER = re.compile(r"\{([A-Za-z0-9_]+)\}")

#: Key prefixes that are English-only in each translation, and why.
ENGLISH_ONLY: dict[str, tuple[str, ...]] = {
    "zh-CN": (
        "nav.adviser", "nav.carrier",            # adviser and insurer workspaces
        "reviewer",                              # the reviewer console
        "shell.role.ADVISER", "shell.role.CARRIER",
        "ask.adviser_workspace", "ask.carrier_workspace",
    ),
}


@dataclass(frozen=True)
class CheckResult:
    missing: tuple[str, ...]
    unused: tuple[str, ...]
    translation_problems: tuple[str, ...] = field(default=())

    @property
    def ok(self) -> bool:
        return not self.missing and not self.unused and not self.translation_problems


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


def _in_scope(key: str, prefixes: tuple[str, ...]) -> bool:
    return any(key == p or key.startswith(p + ".") for p in prefixes)


def check_translation(locale: str, english: dict[str, str],
                      translated: dict[str, str]) -> list[str]:
    """Problems in one translation catalogue, measured against its declared scope."""
    english_only = ENGLISH_ONLY.get(locale, ())
    problems: list[str] = []
    for key in sorted(english):
        if not _in_scope(key, english_only) and key not in translated:
            problems.append(f"{locale}: missing owner-facing translation: {key}")
    for key in sorted(translated):
        if key not in english:
            problems.append(f"{locale}: stale key not in en.json: {key}")
        elif _in_scope(key, english_only):
            problems.append(f"{locale}: key is in an English-only scope: {key}")
        elif set(PLACEHOLDER.findall(translated[key])) != set(PLACEHOLDER.findall(english[key])):
            problems.append(f"{locale}: placeholders differ from English: {key}")
    return problems


def check(root: Path) -> CheckResult:
    root = Path(root)
    i18n_dir = root / "web" / "i18n"
    catalogue_path = i18n_dir / "en.json"
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
    problems: list[str] = []
    for path in sorted(i18n_dir.glob("*.json")):
        locale = path.stem
        if locale == "en":
            continue
        if locale not in ENGLISH_ONLY:
            problems.append(f"{locale}: no declared translation scope in check_i18n.py")
            continue
        try:
            translated = _flatten(json.loads(path.read_text(encoding="utf-8")))
        except (OSError, ValueError, json.JSONDecodeError) as exc:
            raise ValueError(f"cannot read {path}: {exc}") from exc
        problems.extend(check_translation(locale, catalogue, translated))
    keys = set(catalogue)
    return CheckResult(tuple(sorted(calls - keys)), tuple(sorted(keys - calls)),
                       tuple(problems))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
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
    for problem in result.translation_problems:
        print(problem, file=sys.stderr)
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
