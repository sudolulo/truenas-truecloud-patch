"""Guards on the CI workflows themselves.

The workflows run on TWO forges -- Gitea (canonical) and GitHub (mirror), because
Gitea reads .github/workflows too -- and they hold tokens. A mistake here is not a
failed build, it is a bug report nobody files or a command nobody meant to run.
"""

import os
import re

import pytest

ROOT = os.path.join(os.path.dirname(__file__), "..")
WORKFLOWS = os.path.join(ROOT, ".github", "workflows")


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

    def test_compat_files_its_report_through_ONE_implementation(self):
        # It used to be two near-identical shell steps, one per forge. Two copies of
        # "find the issue, decide whether to comment, post it" is two chances to drift,
        # and the Gitea one duplicated an issue for real.
        with open(os.path.join(WORKFLOWS, "compat.yml"), encoding="utf-8") as fh:
            src = fh.read()
        assert "tools/compat_publish.py" in src
        assert "file a bug report (GitHub)" not in src
        assert "file a bug report (Gitea)" not in src


class TestTheBotDoesNotSpam:
    """It left 11 identical 3,000-character comments on one issue in a single day.

    A bot that repeats itself daily gets muted — and then the next REAL finding is
    scrolled past, which defeats the entire reason for building it.
    """

    def publisher(self):
        with open(os.path.join(ROOT, "tools", "compat_publish.py"), encoding="utf-8") as fh:
            return fh.read()

    def test_it_compares_a_fingerprint_before_saying_anything(self):
        src = self.publisher()
        assert "extract_fingerprint" in src
        assert "staying quiet" in src

    def test_the_body_is_edited_in_place_not_appended_to(self):
        src = self.publisher()
        assert '"PATCH"' in src, "the issue body must be updated, not commented onto"

    def test_it_closes_the_issue_when_everything_is_fixed(self):
        src = self.publisher()
        assert '"state": "closed"' in src

    def test_the_matrix_refresh_opens_a_PR_rather_than_pushing_to_main(self):
        # An unattended push to main from CI is exactly what the release barrier exists
        # to prevent: a bot that can move main can move it somewhere nobody looked.
        #
        # Checked against CODE, not comments — the step's own commentary explains what
        # it replaced, and that mention must not read as the thing itself.
        with open(os.path.join(WORKFLOWS, "compat.yml"), encoding="utf-8") as fh:
            code = "\n".join(
                ln for ln in fh.read().splitlines() if not ln.lstrip().startswith("#")
            )
        assert "gh pr create" in code
        assert "HEAD:main" not in code, "CI still pushes straight to main"


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
