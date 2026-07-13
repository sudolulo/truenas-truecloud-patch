#!/usr/bin/env python3
"""Extract one version's section from CHANGELOG.md, and check version consistency.

Used by .github/workflows/release.yml so a release's body is always the changelog
entry -- there is no second place to write release notes, and therefore no second
place for them to be wrong.

    python3 tools/release_notes.py notes v0.3.0     # -> the section body
    python3 tools/release_notes.py version          # -> version per the scripts
    python3 tools/release_notes.py check v0.3.0     # -> exit 1 on any mismatch
"""

from __future__ import annotations

import os
import re
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CHANGELOG = os.path.join(ROOT, "CHANGELOG.md")

# Everything that announces a version must agree with everything else. They drifted
# to three different values once (0.0.4 / 0.2.1) before anything checked them --
# and create_task.py's __version__ then sat at 0.2.0 through three more releases,
# because the first version of this check only looked at VERSION= in shell scripts.
VERSIONED_FILES = [
    "install.sh",
    "uninstall.sh",
    "recover.sh",
    os.path.join("patch", "apply.sh"),
    os.path.join("patch", "create_task.py"),   # exposes `--version` to users
    "update.sh",
]

# `VERSION="x"` (shell) or `__version__ = "x"` (python).
_VERSION_RE = re.compile(r'^(?:VERSION=|__version__\s*=\s*)"([^"]+)"', re.M)
_HEADING_RE = re.compile(r"^##\s+v?(\d+\.\d+\.\d+[^\s]*)", re.M)


def normalise(v: str) -> str:
    return v.strip().lstrip("v")


def script_versions(root: str = ROOT) -> dict[str, str]:
    """VERSION= as declared by each script."""
    found = {}
    for rel in VERSIONED_FILES:
        path = os.path.join(root, rel)
        try:
            with open(path, encoding="utf-8") as fh:
                m = _VERSION_RE.search(fh.read())
        except OSError:
            continue
        if m:
            found[rel] = m.group(1)
    return found


def changelog_versions(text: str) -> list[str]:
    """Versions with a section in the changelog, newest first."""
    return [normalise(v) for v in _HEADING_RE.findall(text)]


def extract_notes(text: str, version: str) -> str:
    """The body of one version's section, without its heading.

    Raises KeyError if the version has no section -- a release with an empty or
    wrong body is worse than a failed release.
    """
    want = normalise(version)
    lines = text.splitlines()

    start = None
    for i, line in enumerate(lines):
        m = _HEADING_RE.match(line)
        if m and normalise(m.group(1)) == want:
            start = i + 1
            break
    if start is None:
        raise KeyError(f"CHANGELOG.md has no section for v{want}")

    end = len(lines)
    for i in range(start, len(lines)):
        if _HEADING_RE.match(lines[i]):
            end = i
            break

    return "\n".join(lines[start:end]).strip()


# ── significance ──────────────────────────────────────────────────────────────
# Used by the TrueNAS update alert to decide whether a release is worth bothering
# anyone about. The CHANGELOG's own section headings are the signal: a release that
# only has "### Docs" changed no code, and nobody should get an alert for a README.

_SECTION_RE = re.compile(r"^###\s+(.+?)\s*$", re.M)

#: Headings that mean "nothing about the running system changed".
QUIET_SECTIONS = {"docs", "documentation"}


def version_tuple(v: str) -> tuple:
    """Sortable version. Pre-release suffixes are dropped, not ranked."""
    return tuple(int(x) for x in normalise(v).split("-")[0].split("."))


def section_headings(body: str) -> list[str]:
    """The `### ...` headings inside one version's body, lowercased."""
    return [h.strip().lower() for h in _SECTION_RE.findall(body)]


def significance(text: str, current: str, latest: str):
    """How much does upgrading `current` -> `latest` actually matter?

    Returns ``(level, versions, headings)`` where level is one of:

      "security"  a release in the range has a Security section  -> alert loudly
      "notable"   something about the system changed             -> alert quietly
      "docs"      only documentation changed                     -> DO NOT alert

    Considers every release in the range, not just the newest: a docs-only v0.4.2
    on top of a security-fixing v0.4.1 must still be reported as security.
    """
    cur, lat = version_tuple(current), version_tuple(latest)

    versions = [
        v for v in changelog_versions(text)
        if cur < version_tuple(v) <= lat
    ]

    headings = []
    for v in versions:
        try:
            headings.extend(section_headings(extract_notes(text, v)))
        except KeyError:
            continue

    if any(h.startswith("security") for h in headings):
        return "security", versions, headings
    if [h for h in headings if h not in QUIET_SECTIONS]:
        return "notable", versions, headings
    return "docs", versions, headings


def check(version: str, root: str = ROOT) -> list[str]:
    """Every reason this version is not releasable. Empty list means it is."""
    want = normalise(version)
    problems = []

    versions = script_versions(root)
    for rel, got in sorted(versions.items()):
        if normalise(got) != want:
            problems.append(f"{rel} declares VERSION={got!r}, tag is v{want}")
    missing = [r for r in VERSIONED_FILES if r not in versions]
    for rel in missing:
        problems.append(f"{rel} has no VERSION= line")

    try:
        with open(os.path.join(root, "CHANGELOG.md"), encoding="utf-8") as fh:
            text = fh.read()
    except OSError as e:
        problems.append(f"cannot read CHANGELOG.md: {e}")
        return problems

    try:
        body = extract_notes(text, want)
    except KeyError as e:
        problems.append(str(e))
    else:
        if not body:
            problems.append(f"CHANGELOG.md section for v{want} is empty")

    return problems


def main(argv):
    if len(argv) < 2:
        print(__doc__, file=sys.stderr)
        return 2

    cmd = argv[1]

    if cmd == "version":
        versions = set(map(normalise, script_versions().values()))
        if len(versions) != 1:
            print(f"scripts disagree on version: {sorted(versions)}", file=sys.stderr)
            return 1
        print(versions.pop())
        return 0

    if len(argv) < 3:
        print(f"usage: {argv[0]} {cmd} <version>", file=sys.stderr)
        return 2
    version = argv[2]

    if cmd == "notes":
        # An explicit path lets update.sh show the notes from the CHANGELOG of the
        # version it is about to install (`git show <tag>:CHANGELOG.md`), not the
        # one already checked out.
        path = argv[3] if len(argv) > 3 else CHANGELOG
        with open(path, encoding="utf-8") as fh:
            print(extract_notes(fh.read(), version))
        return 0

    if cmd == "check":
        problems = check(version)
        for p in problems:
            print(f"::error::{p}")
        if problems:
            return 1
        print(f"v{normalise(version)} is consistent across scripts and CHANGELOG")
        return 0

    print(f"unknown command: {cmd}", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(main(sys.argv))
