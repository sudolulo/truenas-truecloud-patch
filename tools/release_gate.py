#!/usr/bin/env python3
"""The barrier: a stable release must have been a release candidate first.

CHECKS
------
release_notes.py checks the *content* of the tree (versions agree, CHANGELOG has a
non-empty section, nothing stranded under Unreleased). This module checks the
*provenance* of the commit: was this exact code ever a release candidate, and did
that candidate pass CI?

WHY
---
This repo cut twelve releases in a single day, several of them "fix the thing the
last release broke". With an update alert live on every user's box, that is not
iteration, it is nagging -- and it teaches people to ignore the alert that will one
day carry a real security fix.

The rule that makes the bad path impossible:

    A stable vX.Y.Z tag is only publishable if a vX.Y.Z-rcN tag points at the SAME
    commit, and that candidate's CI run passed.

Release candidates are invisible to users: update.sh and the alert source both take
the newest plain vX.Y.Z tag, so an rc is never offered as an update. Debugging
therefore happens across rc1, rc2, rc3 -- where it costs nobody anything -- instead
of across v0.5.0, v0.5.1, v0.5.2, where it costs everybody an alert.

The commit must be *identical*, not merely an ancestor. "The rc passed, then I
pushed one more little fix" is exactly the habit this exists to break.

    python3 tools/release_gate.py v0.6.0        # exit 1 if not promotable
    python3 tools/release_gate.py v0.6.0 --next-rc   # -> the rc tag to cut next
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys

from release_notes import base_version, is_prerelease, normalise


def _git(*args: str, cwd: str | None = None) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=cwd, capture_output=True, text=True, check=True,
    ).stdout.strip()


def rc_tags(version: str, cwd: str | None = None) -> list[str]:
    """Every rc tag for this version, oldest first (rc2 sorts after rc1).

    The glob is only a prefilter; the anchored regex decides. `v1.0.0-rc*` also
    matches `v1.0.0-rc1-hotfix`, which _rc_number reads as 0 -- so a tag the
    numbering logic does not understand could satisfy the barrier while never having
    been a release candidate.
    """
    want = re.escape(normalise(base_version(version)))
    exact = re.compile(rf"^v{want}-rc\d+$")

    out = _git("tag", "--list", f"v{normalise(base_version(version))}-rc*", cwd=cwd)
    tags = [t.strip() for t in out.splitlines() if exact.match(t.strip())]
    return sorted(tags, key=_rc_number)


def _rc_number(tag: str) -> int:
    m = re.search(r"-rc(\d+)$", tag)
    return int(m.group(1)) if m else 0


def next_rc(version: str, cwd: str | None = None) -> str:
    """The next rc tag to cut: v0.6.0-rc1, then -rc2, ..."""
    existing = rc_tags(version, cwd=cwd)
    n = max((_rc_number(t) for t in existing), default=0) + 1
    return f"v{normalise(base_version(version))}-rc{n}"


def commit_for(ref: str, cwd: str | None = None) -> str | None:
    try:
        return _git("rev-list", "-n", "1", ref, cwd=cwd)
    except subprocess.CalledProcessError:
        return None


def check_promotable(version: str, cwd: str | None = None) -> list[str]:
    """Every reason v<version> may not be cut as a stable release.

    Empty list means the barrier is satisfied.

    The commit under test is the tag's if it exists, and HEAD otherwise. Both are
    real: CI runs this AFTER the tag is pushed, and release.sh runs it BEFORE
    creating the tag -- which is the whole point, since refusing after the tag
    exists is too late to be a gate. Requiring the tag unconditionally made
    `release.sh --promote` impossible: it dies if the tag already exists, and the
    gate died if it did not, so the only way through was to hand-tag and bypass
    every check this file exists to enforce.
    """
    if is_prerelease(version):
        return []  # candidates are what the barrier exists to encourage

    want = normalise(version)
    tag = f"v{want}"

    target = commit_for(tag, cwd=cwd)
    if target is None:
        target = commit_for("HEAD", cwd=cwd)
    if target is None:
        return ["cannot resolve a commit to release (no HEAD?)"]

    candidates = rc_tags(want, cwd=cwd)
    if not candidates:
        return [
            f"{tag} was never a release candidate. Cut one first:\n"
            f"    bash release.sh {want} --rc\n"
            f"Candidates are invisible to users -- debug there, not in a release."
        ]

    matching = [c for c in candidates if commit_for(c, cwd=cwd) == target]
    if not matching:
        newest = candidates[-1]
        return [
            f"{tag} points at {target[:12]}, but no release candidate does.\n"
            f"    Candidates: {', '.join(candidates)}\n"
            f"    Newest ({newest}) is at "
            f"{(commit_for(newest, cwd=cwd) or '?')[:12]}.\n"
            f"Code changed after the last candidate. That change is untested as a\n"
            f"release: cut {next_rc(want, cwd=cwd)} and promote THAT commit."
        ]

    return []


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("version", help="e.g. v0.6.0")
    ap.add_argument("--next-rc", action="store_true",
                    help="print the next rc tag to cut, and exit")
    ap.add_argument("-C", dest="cwd", default=None, help="run git in this directory")
    args = ap.parse_args(argv[1:])

    if args.next_rc:
        print(next_rc(args.version, cwd=args.cwd))
        return 0

    problems = check_promotable(args.version, cwd=args.cwd)
    for p in problems:
        print(f"::error::{p}")
    if problems:
        return 1

    print(f"v{normalise(args.version)} was a release candidate and may be promoted")
    return 0


if __name__ == "__main__":
    # Running `python3 tools/release_gate.py` already puts tools/ on sys.path[0],
    # which is what makes the `release_notes` import above resolve.
    sys.exit(main(sys.argv))
