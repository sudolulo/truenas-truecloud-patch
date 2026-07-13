"""The docs must not lie about themselves.

The README was 969 lines with the install instructions at line 517. Splitting it into
docs/ fixed that and broke every cross-reference in the process -- which is the normal
outcome of moving Markdown around, and exactly why this is a test rather than a
careful afternoon.

A dead link in a recovery doc is worse than a dead link anywhere else: the person
following it is, by definition, already having a bad day.
"""

import os
import re

import pytest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DOCS = os.path.join(ROOT, "docs")

LINK_RE = re.compile(r"\[([^\]]+)\]\(([^)]+)\)")
HEADING_RE = re.compile(r"^#{1,6}\s+(.*)$", re.M)


def markdown_files():
    files = [os.path.join(ROOT, "README.md"), os.path.join(ROOT, "CHANGELOG.md")]
    if os.path.isdir(DOCS):
        files += [os.path.join(DOCS, f) for f in sorted(os.listdir(DOCS))
                  if f.endswith(".md")]
    return files


def anchors(text):
    """GitHub/Gitea slugs for every heading in `text`."""
    out = set()
    for h in HEADING_RE.findall(text):
        slug = re.sub(r"[^a-z0-9 -]", "", h.lower()).replace(" ", "-")
        out.add(slug)
    return out


@pytest.mark.parametrize("path", markdown_files(), ids=os.path.basename)
def test_every_internal_link_resolves(path):
    with open(path, encoding="utf-8") as fh:
        text = fh.read()
    here = anchors(text)
    base = os.path.dirname(path)

    broken = []
    for label, target in LINK_RE.findall(text):
        if target.startswith(("http://", "https://", "mailto:")):
            continue
        rel, _, anchor = target.partition("#")

        if not rel:                                  # same-file anchor
            if anchor and anchor not in here:
                broken.append(f"[{label}](#{anchor}) — no such heading here")
            continue

        dest = os.path.normpath(os.path.join(base, rel))
        if not os.path.exists(dest):
            broken.append(f"[{label}]({target}) — file does not exist")
            continue

        if anchor and dest.endswith(".md"):
            with open(dest, encoding="utf-8") as fh:
                if anchor not in anchors(fh.read()):
                    broken.append(f"[{label}]({target}) — no such heading there")

    assert not broken, "broken links in {}:\n  {}".format(
        os.path.basename(path), "\n  ".join(broken)
    )


class TestTheReadmeStaysAReadme:
    def test_install_is_near_the_top(self):
        # It was at line 517 of 969, under a wall of internals. Somebody deciding
        # whether to use this should not have to scroll past the boot sequence.
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            lines = fh.read().splitlines()
        install = next(i for i, ln in enumerate(lines, 1) if ln.startswith("## Install"))
        assert install < 40, f"## Install is at line {install}"

    def test_the_readme_does_not_grow_back(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            n = len(fh.read().splitlines())
        assert n < 300, (
            f"README is {n} lines. Detail belongs in docs/ — the README is what "
            f"someone reads before they trust this with their backups."
        )

    def test_the_minimum_version_is_stated_before_the_install_command(self):
        with open(os.path.join(ROOT, "README.md"), encoding="utf-8") as fh:
            text = fh.read()
        assert "24.10" in text[:text.index("## Install")], (
            "the minimum TrueNAS version must be visible above the install steps"
        )


class TestInstallDoesNotDirtyTheCheckout:
    """install.sh chmod +x's scripts. If git records them as 100644, that chmod is a
    TRACKED MODIFICATION -- and update.sh refuses to run over a dirty tree.

    So installing once permanently blocked updating, for every user, with a message
    telling them to `git checkout -- .` (which would just undo the exec bit and let
    the next install re-dirty it). Found on a real box that had been stuck on an old
    version for exactly this reason.

    Every script install.sh makes executable must already be executable in git.
    """

    def test_every_chmodded_script_is_already_executable_in_git(self):
        import re
        import subprocess

        with open(os.path.join(ROOT, "install.sh"), encoding="utf-8") as fh:
            m = re.search(r"^for _exe in (.+?); do", fh.read(), re.M)
        assert m, "could not find install.sh's chmod loop"
        scripts = m.group(1).split()

        out = subprocess.run(
            ["git", "ls-files", "-s", *scripts],
            cwd=ROOT, capture_output=True, text=True, check=True,
        ).stdout

        not_exec = [
            line.split("\t")[-1] for line in out.strip().splitlines()
            if not line.startswith("100755")
        ]
        assert not not_exec, (
            "install.sh chmod +x's these, but git records them as non-executable — "
            "so installing dirties the checkout and update.sh then refuses to run:\n  "
            + "\n  ".join(not_exec)
        )
