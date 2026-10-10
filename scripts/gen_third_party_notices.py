#!/usr/bin/env python
"""Regenerate THIRD_PARTY_NOTICES.md from what is actually installed.

    python scripts/gen_third_party_notices.py            # write THIRD_PARTY_NOTICES.md
    python scripts/gen_third_party_notices.py --check    # exit 1 if it is out of date

WHY GENERATED. A hand-written notices file is accurate on the day it is typed and wrong
the first time a pin moves. This reads the pinned runtime requirements from
`requirements.txt`, walks their installed dependency closure through package metadata,
and reads each distribution's licence and licence texts — so the file describes the
packages a deployment really ships, at the versions it really ships.

WHAT IS IN IT. Every RUNTIME distribution and everything it pulls in, with every licence
file it declares and its NOTICE file where the package ships one: these are
redistributed by anyone who builds an image from this repository. Development-only tools
(the test runner, this generator) are listed by name, version and licence without their
texts — they are not part of a deployment.

WHERE THE LICENCE TEXTS COME FROM. The distribution's own core metadata first: each
`License-File:` entry (PEP 639) names a file the distribution ships for exactly this
purpose, and every one of them is reproduced. `pip-licenses` is the fallback for
distributions whose metadata lists none. It is not enough on its own, and relying on it
alone was a real defect: it returns ONE file, and only one named like LICENSE* or
COPYING*. pypdfium2 names its eighteen licence files by SPDX identifier and by bundled
library — the licences of the PDFium binary and of FreeType, ICU, libjpeg-turbo and the
rest compiled into it — so it got none at all, and the file said it shipped none.
packaging and sniffio ship a short pointer file next to the licences it points to, and
got only the pointer.

ONE PLATFORM PER FILE. The file says at the top which platform it describes, because
two things in it depend on the platform: environment markers decide which packages are
installed (tqdm requires colorama only where `platform_system == "Windows"`), and a
binary wheel's compiled code — and the native libraries it bundles, with their
licence files — are built per platform. A deployment on another platform regenerates
the file there. `--check` refuses to compare a file generated for one platform with an
environment on another, rather than reporting that difference as staleness.

Run it inside the project's virtual environment after `pip install -r requirements.txt`;
it describes the environment it runs in. That environment is reproducible because
requirements.txt applies constraints.txt, which pins every package of the runtime and
development closures. Without it the file recorded versions of packages nobody had
pinned, and a clean install would no longer match it once any of them released.
"""
from __future__ import annotations

import argparse
import json
import platform
import re
import subprocess
import sys
import sysconfig
from importlib import metadata
from pathlib import Path

REPO = Path(__file__).resolve().parent.parent
REQUIREMENTS = REPO / "requirements.txt"
OUTPUT = REPO / "THIRD_PARTY_NOTICES.md"

#: The line in requirements.txt after which everything is development-only.
DEV_MARKER = "development and tests"

#: Written for a runtime distribution with no licence text at all. A test fails on it:
#: NOTICE promises the licence text of every runtime component.
NO_LICENCE_TEXT = "(the distribution ships no licence file)"

#: Environment-marker variables that name the PLATFORM rather than the interpreter
#: version. A dependency reached only through a marker on one of these is installed on
#: some platforms and not on others.
PLATFORM_VARIABLES = ("os_name", "sys_platform", "platform_system", "platform_machine",
                      "platform_release", "platform_version",
                      "platform_python_implementation", "implementation_name")

_NAME = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._-]*)")
_DESCRIBES = re.compile(r"^Describes: (.+?)\.$", re.MULTILINE)


def canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


def pinned_requirements(path: Path = REQUIREMENTS) -> tuple[list[str], list[str]]:
    """(runtime, dev) distribution names, in file order."""
    runtime: list[str] = []
    dev: list[str] = []
    target = runtime
    for line in path.read_text(encoding="utf-8").splitlines():
        if line.lstrip().startswith("#"):
            if DEV_MARKER in line:
                target = dev
            continue
        m = _NAME.match(line)
        if m and "==" in line:
            target.append(m.group(1))
    return runtime, dev


# ── the platform ────────────────────────────────────────────────────────────

def target_platform() -> str:
    """The platform this environment is: interpreter, its version, and platform tag."""
    return (f"{platform.python_implementation()} "
            f"{sys.version_info.major}.{sys.version_info.minor} on "
            f"{sysconfig.get_platform()}")


def described_platform(text: str) -> str | None:
    """The platform a generated notices file says it describes, if it says."""
    m = _DESCRIBES.search(text)
    return m.group(1) if m else None


def names_platform(marker: str) -> bool:
    return any(re.search(rf"\b{v}\b", marker) for v in PLATFORM_VARIABLES)


def wheel_tags(name: str) -> list[str]:
    """The platform-specific tags of an installed binary wheel; [] if pure Python."""
    try:
        text = metadata.distribution(name).read_text("WHEEL") or ""
    except metadata.PackageNotFoundError:
        return []
    tags = [line.split(":", 1)[1].strip() for line in text.splitlines()
            if line.startswith("Tag:")]
    return [t for t in tags if not t.endswith("-any")]


# ── the dependency closure ──────────────────────────────────────────────────

def closure(roots: list[str]) -> list[str]:
    """Every installed distribution `roots` need, following environment markers."""
    installed, _ = resolve(roots)
    return sorted(installed.values(), key=str.lower)


def resolve(roots: list[str]) -> tuple[dict[str, str], dict[str, list[tuple[str, str]]]]:
    """(installed, required_by) for the closure of `roots`.

    `installed` maps canonical name -> distribution name. `required_by` maps canonical
    name -> every (requirer, marker) edge that pulled it in, kept so the file can say WHY
    a platform-dependent package is there rather than only that it is.
    """
    installed: dict[str, str] = {}
    required_by: dict[str, list[tuple[str, str]]] = {}
    todo: list[tuple[str, str, str]] = [(name, "", "") for name in roots]
    while todo:
        name, requirer, marker = todo.pop()
        key = canonical(name)
        if requirer:
            required_by.setdefault(key, []).append((requirer, marker))
        if key in installed:
            continue
        try:
            dist = metadata.distribution(name)
        except metadata.PackageNotFoundError:
            continue
        installed[key] = dist.metadata["Name"]
        for req in dist.requires or []:
            spec, _, marker = req.partition(";")
            marker = marker.strip()
            if marker and not _marker_holds(marker):
                continue
            m = _NAME.match(spec)
            if m:
                todo.append((m.group(1), installed[key], marker))
    return installed, required_by


def platform_selected(roots: list[str], installed: dict[str, str],
                      required_by: dict[str, list[tuple[str, str]]]
                      ) -> dict[str, list[tuple[str, str]]]:
    """The distributions installed here ONLY because a platform marker held.

    One is platform-independent when some chain of requirements from a pinned root
    reaches it without passing a platform marker; everything else in the closure is on
    this platform's account.
    """
    independent = {canonical(r) for r in roots} & set(installed)
    grew = True
    while grew:
        grew = False
        for key, edges in required_by.items():
            if key in independent or key not in installed:
                continue
            if any(canonical(req) in independent and not names_platform(marker)
                   for req, marker in edges):
                independent.add(key)
                grew = True
    return {key: edges for key, edges in sorted(required_by.items())
            if key in installed and key not in independent}


def _marker_holds(marker: str) -> bool:
    """Evaluate an environment marker; an `extra ==` marker is an optional extra."""
    if "extra" in marker:
        return False
    try:
        from packaging.markers import Marker
        return Marker(marker.strip()).evaluate()
    except Exception:
        return False


# ── licence texts ───────────────────────────────────────────────────────────

def licence_files(name: str) -> list[tuple[str, str]]:
    """(declared path, text) for every `License-File` the distribution's metadata names.

    Read in full and in order. A declared file that is not installed is an error, not a
    gap to step over: a notices file that silently drops a licence it was told about is
    the defect this function exists to end.
    """
    dist = metadata.distribution(name)
    out: list[tuple[str, str]] = []
    for declared in dist.metadata.get_all("License-File") or []:
        # PEP 639 keeps them under .dist-info/licenses/; older metadata, at its top.
        text = _dist_info_text(dist, f"licenses/{declared}")
        if text is None:
            text = _dist_info_text(dist, declared)
        if text is None:
            raise RuntimeError(
                f"{dist.metadata['Name']} declares the licence file {declared!r} but it is "
                f"not installed; reinstall the package and regenerate")
        out.append((declared, text))
    return out


def _dist_info_text(dist: metadata.Distribution, relative: str) -> str | None:
    """A file inside the distribution's .dist-info directory, decoded; None if absent."""
    files = dist.files
    if files is None:                    # no RECORD to consult: ask the distribution
        text = dist.read_text(relative)
        return None if text is None else _normalise(text)
    for entry in files:
        head, _, tail = entry.as_posix().partition("/")
        if head.endswith(".dist-info") and tail == relative:
            return _normalise(_decode(entry.read_binary()))
    return None


def _decode(data: bytes) -> str:
    """Licence files are mostly UTF-8. Not all: FreeType's is Latin-1 (its copyright
    sign is the single byte 0xA9), so a UTF-8-only read drops it with an exception."""
    for encoding in ("utf-8-sig", "cp1252"):
        try:
            return data.decode(encoding)
        except UnicodeDecodeError:
            continue
    return data.decode("latin-1")


def _normalise(text: str) -> str:
    return text.replace("\r\n", "\n").replace("\r", "\n")


def licence_texts(row: dict) -> list[tuple[str, str]]:
    """(label, text) blocks for one runtime distribution: every declared licence file,
    plus whatever pip-licenses found if it is not one of them. Empty if there are none."""
    blocks = [(path, _comparable(text)) for path, text in licence_files(row["Name"])
              if _comparable(text)]
    found = _comparable(row.get("LicenseText") or "")
    if found and found != "UNKNOWN" and all(found != text for _, text in blocks):
        blocks.append(("", found))
    return blocks


def _comparable(text: str) -> str:
    """One form for a text however it was read: no byte-order mark, LF line endings,
    no surrounding blank lines, and no trailing whitespace that would make a generated
    notice fail a clean-diff check without changing the licence terms."""
    normalised = _normalise(text).lstrip("\ufeff")
    return "\n".join(line.rstrip() for line in normalised.split("\n")).strip()


def licence_rows(packages: list[str]) -> list[dict]:
    if not packages:
        return []
    cmd = [sys.executable, "-m", "piplicenses", "--format=json", "--with-urls",
           "--with-license-file", "--no-license-path", "--with-notice-file",
           "--packages", *packages]
    out = subprocess.run(cmd, capture_output=True, text=True, encoding="utf-8",
                         check=True).stdout
    return sorted(json.loads(out), key=lambda r: r["Name"].lower())


# ── rendering ───────────────────────────────────────────────────────────────

def _platform_section(runtime: list[dict],
                      selected: dict[str, list[tuple[str, str]]]) -> list[str]:
    lines = [
        "## Platform",
        "",
        f"Describes: {target_platform()}.",
        "",
        "This file is generated from an installed environment, and two things in it "
        "depend on the platform that environment runs on: which packages environment "
        "markers select, and what a binary wheel compiles in and bundles, licence files "
        "included. The entries below are the ones that differ on another platform. A "
        "deployment on any other platform — a Linux container image, for one — "
        "regenerates this file there; `--check` refuses to compare a file generated for "
        "one platform with an environment on another.",
        "",
    ]
    rows = {canonical(r["Name"]): r for r in runtime}
    if selected:
        lines += ["Installed only because a platform marker holds here:", ""]
        for key, edges in selected.items():
            row = rows[key]
            markers = sorted({m for _, m in edges if names_platform(m)})
            requirers = sorted({req for req, _ in edges}, key=str.lower)
            why = (", ".join(f"`{m}`" for m in markers)
                   or "required only by packages that are themselves platform-selected")
            lines.append(f"- {row['Name']} {row['Version']} — {why} "
                         f"(required by {', '.join(requirers)})")
        lines.append("")
    binaries = [(r, wheel_tags(r["Name"])) for r in runtime]
    binaries = [(r, tags) for r, tags in binaries if tags]
    if binaries:
        lines += ["Binary wheels, built for this platform:", ""]
        for r, tags in binaries:
            lines.append(f"- {r['Name']} {r['Version']} — "
                         + ", ".join(f"`{t}`" for t in tags))
        lines.append("")
    return lines


def render(runtime: list[dict], dev: list[dict],
           selected: dict[str, list[tuple[str, str]]] | None = None) -> str:
    lines = [
        "# Third-party notices",
        "",
        "Generated by `scripts/gen_third_party_notices.py` from the installed environment "
        "and the pins in `requirements.txt` and `constraints.txt`, so every version below "
        "is the one a fresh install gets. Do not edit by hand: regenerate it. What each "
        "component does in VERIS is described in [OPEN-SOURCE.md](OPEN-SOURCE.md).",
        "",
        "VERIS itself is licensed under the Apache License, Version 2.0 — see "
        "[LICENSE](LICENSE) and [NOTICE](NOTICE).",
        "",
        *_platform_section(runtime, selected or {}),
        "## Runtime components",
        "",
        "Shipped by any deployment built from this repository: the pinned runtime "
        "requirements and everything they depend on.",
        "",
        "| Package | Version | Licence | Project URL |",
        "|---|---|---|---|",
    ]
    for r in runtime:
        lines.append(f"| {r['Name']} | {r['Version']} | {_cell(r['License'])} | "
                     f"{_cell(r.get('URL', ''))} |")
    lines += [
        "",
        "## Development-only tools",
        "",
        "Used to test and build the repository. Not part of a deployment, so their licence "
        "texts are not reproduced here.",
        "",
        "| Package | Version | Licence |",
        "|---|---|---|",
    ]
    for r in dev:
        lines.append(f"| {r['Name']} | {r['Version']} | {_cell(r['License'])} |")
    lines += ["", "## Licence texts of the runtime components", ""]
    for r in runtime:
        lines += [f"### {r['Name']} {r['Version']}", "",
                  f"Licence: {_cell(r['License'])}", ""]
        blocks = r.get("texts") or []
        if not blocks:
            lines += ["```text", NO_LICENCE_TEXT, "```", ""]
        for label, text in blocks:
            # One text needs no label. Several are told apart by the file each came from,
            # which is how a reader matches a bundled library to its terms.
            if len(blocks) > 1:
                lines += [f"From `{label}`:" if label
                          else "Found by pip-licenses, not declared in the metadata:", ""]
            lines += ["```text", _fence_safe(text), "```", ""]
        notice = (r.get("NoticeText") or "").strip()
        if notice and notice != "UNKNOWN":
            lines += ["NOTICE:", "", "```text", _fence_safe(notice), "```", ""]
    return "\n".join(lines).rstrip() + "\n"


def _cell(value: str) -> str:
    return (value or "UNKNOWN").replace("|", "/").replace("\n", " ").strip()


def _fence_safe(text: str) -> str:
    return text.replace("```", "'''")


def _metadata_row(name: str) -> dict:
    """A row read straight from package metadata.

    `pip-licenses` leaves itself and its own dependencies out of its report by design.
    They are development tools here, so they are listed from their metadata instead of
    silently missing from the table.
    """
    meta = metadata.distribution(name).metadata
    licence = (meta.get("License-Expression") or meta.get("License") or "").strip()
    if not licence or len(licence) > 80:
        classifiers = [c.split("::")[-1].strip() for c in meta.get_all("Classifier") or []
                       if c.startswith("License ::")]
        licence = "; ".join(classifiers) or "UNKNOWN"
    return {"Name": meta["Name"], "Version": meta["Version"], "License": licence}


def build() -> str:
    runtime_roots, dev_roots = pinned_requirements()
    installed, required_by = resolve(runtime_roots)
    runtime = sorted(installed.values(), key=str.lower)
    runtime_keys = {canonical(n) for n in runtime}
    dev = [n for n in closure(dev_roots) if canonical(n) not in runtime_keys]
    dev_rows = licence_rows(dev)
    reported = {canonical(r["Name"]) for r in dev_rows}
    dev_rows += [_metadata_row(n) for n in dev if canonical(n) not in reported]
    dev_rows.sort(key=lambda r: r["Name"].lower())
    runtime_rows = licence_rows(runtime)
    reported = {canonical(r["Name"]) for r in runtime_rows}
    runtime_rows += [_metadata_row(n) for n in runtime if canonical(n) not in reported]
    runtime_rows.sort(key=lambda r: r["Name"].lower())
    for row in runtime_rows:
        row["texts"] = licence_texts(row)
    selected = platform_selected(runtime_roots, installed, required_by)
    return render(runtime_rows, dev_rows, selected)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--check", action="store_true",
                    help="exit 1 if THIRD_PARTY_NOTICES.md differs from a fresh build")
    args = ap.parse_args(argv)
    if args.check:
        current = OUTPUT.read_text(encoding="utf-8") if OUTPUT.exists() else ""
        there, here = described_platform(current), target_platform()
        if there is not None and there != here:
            print(f"THIRD_PARTY_NOTICES.md describes {there}; this environment is {here}. "
                  "It is compared only on the platform it was generated for: regenerate "
                  "it here if this is the platform being deployed.")
            return 1
        if current != build():
            print("THIRD_PARTY_NOTICES.md is out of date; run "
                  "scripts/gen_third_party_notices.py")
            return 1
        print("THIRD_PARTY_NOTICES.md is up to date")
        return 0
    OUTPUT.write_text(build(), encoding="utf-8", newline="\n")
    print(f"wrote {OUTPUT.relative_to(REPO)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
