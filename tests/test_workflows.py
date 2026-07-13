"""Guards on the CI workflows themselves.

The workflows run on TWO forges -- Gitea (canonical) and GitHub (mirror), because
Gitea reads .github/workflows too -- and they hold tokens. A mistake here is not a
failed build, it is a bug report nobody files or a command nobody meant to run.
"""

import os
import re

import pytest

WORKFLOWS = os.path.join(os.path.dirname(__file__), "..", ".github", "workflows")


def workflow_files():
    return [
        os.path.join(WORKFLOWS, f)
        for f in sorted(os.listdir(WORKFLOWS))
        if f.endswith((".yml", ".yaml"))
    ]


def run_bodies(path):
    """Every `run:` block's text, with its line number."""
    with open(path, encoding="utf-8") as fh:
        lines = fh.readlines()

    out = []
    i = 0
    while i < len(lines):
        m = re.match(r"^(\s*)run:\s*\|", lines[i])
        if not m:
            i += 1
            continue
        indent = len(m.group(1))
        start = i + 1
        body = []
        i += 1
        while i < len(lines):
            line = lines[i]
            if line.strip() and (len(line) - len(line.lstrip())) <= indent:
                break
            body.append(line)
            i += 1
        out.append((start + 1, "".join(body)))
    return out


class TestNoExpressionInterpolationIntoShell:
    """`${{ ... }}` inside a `run:` body is spliced into the SCRIPT TEXT.

    This is not theoretical. `echo "${{ steps.report.outputs.body }}"` in the compat
    workflow pasted the report -- which is full of backticks -- straight into bash,
    which promptly ran `create-snapshot`, `def` and `async` as commands. And because
    that report is built from iX's middleware source, anything landing in their tree
    would have executed on our runner.

    The rule: files for data, `env:` for scalars. `env:` is safe because the runner
    sets the variable rather than pasting it into the script.
    """

    @pytest.mark.parametrize("path", workflow_files(), ids=os.path.basename)
    def test_no_github_expression_in_a_run_body(self, path):
        offenders = []
        for lineno, body in run_bodies(path):
            for m in re.finditer(r"\$\{\{[^}]*\}\}", body):
                offenders.append(f"{os.path.basename(path)}:~{lineno}: {m.group(0)}")
        assert not offenders, (
            "GitHub/Gitea expressions interpolate into the shell script text, so "
            "backticks and $() in the value EXECUTE. Pass data via a file, or a "
            "scalar via `env:`.\n  " + "\n  ".join(offenders)
        )


class TestBothForges:
    """Gitea is canonical; GitHub is a mirror. Both run these files."""

    def test_release_publishes_on_each_forge_exactly_once(self):
        with open(os.path.join(WORKFLOWS, "release.yml"), encoding="utf-8") as fh:
            src = fh.read()
        # One step gated ON github.com, one gated OFF it. Without the pair, a release
        # either double-publishes or silently never publishes on the canonical host.
        assert "if: ${{ contains(github.server_url, 'github.com') }}" in src
        assert "if: ${{ !contains(github.server_url, 'github.com') }}" in src

    def test_compat_files_an_issue_on_each_forge(self):
        with open(os.path.join(WORKFLOWS, "compat.yml"), encoding="utf-8") as fh:
            src = fh.read()
        assert "file a bug report (GitHub)" in src
        assert "file a bug report (Gitea)" in src


class TestCompatCannotSilentlyPass:
    def test_the_exit_code_is_captured_not_swallowed(self):
        # Actions runs `bash -e`: `cmd > out` followed by `echo $?` never reaches the
        # echo, so the "a shipped release is broken" signal would be lost and the job
        # would go green while users were broken.
        with open(os.path.join(WORKFLOWS, "compat.yml"), encoding="utf-8") as fh:
            src = fh.read()
        assert "|| rc=$?" in src
        assert "shipped_broken=$rc" in src

    def test_a_broken_shipped_release_fails_the_job(self):
        with open(os.path.join(WORKFLOWS, "compat.yml"), encoding="utf-8") as fh:
            src = fh.read()
        assert "steps.check.outputs.shipped_broken != '0'" in src
