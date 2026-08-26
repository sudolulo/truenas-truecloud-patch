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
            src = fh.read()
        code = "\n".join(
            ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
        )
        assert "/pulls" in code, "the matrix refresh must open a PR"
        assert "HEAD:main" not in code, "CI still pushes straight to main"

    def test_the_matrix_PR_targets_the_CANONICAL_forge_not_the_mirror(self):
        # GitHub is a one-way mirror: a PR merged there would be silently clobbered by
        # the next `fleet-repos mirror` push from Gitea. A bot opening PRs against a
        # mirror is a bot doing nothing, slowly.
        with open(os.path.join(WORKFLOWS, "compat.yml"), encoding="utf-8") as fh:
            src = fh.read()
        i = src.index("refresh the README matrix")
        step = src[i:i + 400]
        assert "!contains(github.server_url, 'github.com')" in step, (
            "the matrix PR must be opened on Gitea (canonical), not GitHub (mirror)"
        )

    def test_the_workflow_has_the_permissions_its_steps_actually_need(self):
        # It shipped with `contents: read` while the step pushed a branch and opened a
        # PR — it would have died with a 403 on the first scheduled run, and I would
        # have had a bot that silently never worked.
        with open(os.path.join(WORKFLOWS, "compat.yml"), encoding="utf-8") as fh:
            src = fh.read()
        perms = src[src.index("permissions:"):src.index("jobs:")]
        assert "contents: write" in perms, "pushing a branch needs contents: write"
        assert "pull-requests: write" in perms, "opening a PR needs pull-requests: write"
        assert "issues: write" in perms


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


class TestActionCacheRace:
    """CI must not run concurrent jobs on the self-hosted runner.

    `act` caches each ACTION as one shared clone under /root/.cache/act/<hash>
    and re-pulls it per job, so jobs starting together fight over that directory
    and the loser dies with `lstat .../<file>: no such file or directory` before
    any test runs -- a red `main` with zero suite output and a different victim
    each push. The runner also force-pulls its base image per job, so job count
    is also Docker Hub pull count, and four-per-push exhausted the anonymous
    limit in an afternoon. Both problems have the same cure: one job.
    """

    def _ci(self):
        with open(os.path.join(WORKFLOWS, "ci.yml"), encoding="utf-8") as fh:
            return fh.read()

    def test_ci_runs_as_exactly_one_job(self):
        """The fix is the absence of concurrency, not the absence of one action.

        Dropping astral-sh/setup-uv only shrank the surface -- every job still
        used actions/checkout. A single job cannot race itself whatever actions
        it uses, which is why this, and not the action count, is the invariant.
        """
        ci = self._ci()
        # Scope to the jobs: block -- `on:` has two-space keys of its own
        # (push/pull_request/workflow_dispatch) that look identical otherwise.
        body = ci[ci.index("\njobs:"):]
        jobs = re.findall(r"^  (\w[\w-]*):$", body, re.M)
        assert len(jobs) == 1, (
            f"ci.yml defines {len(jobs)} jobs ({jobs}); concurrent jobs on the "
            "self-hosted runner race on act's shared action cache and multiply "
            "Docker Hub pulls. Keep CI to one job."
        )

    def test_no_matrix_reintroduces_parallel_jobs(self):
        ci = self._ci()
        assert "strategy:" not in ci and "matrix:" not in ci, (
            "a matrix fans out into concurrent jobs again -- sweep versions "
            "inside one job instead"
        )

    def test_every_python_version_still_runs_after_one_fails(self):
        """`fail-fast: false` is what the loop has to preserve.

        A 3.11 break must not hide whether 3.12 and 3.13 are fine; that is
        precisely the information you want at that moment.
        """
        ci = self._ci()
        assert 'PYTHONS: "3.11 3.12 3.13"' in ci
        assert ci.count("fail=1") >= 2, "the sweeps must collect failures, not exit early"

    def test_uv_is_installed_without_an_action(self):
        """Checks `uses:` directives, not prose.

        The comment in ci.yml names the action it deliberately avoids, and that
        explanation is the most useful thing in the file -- a test that greps the
        raw text would forbid documenting the very lesson it enforces. Parsed
        with a regex rather than PyYAML on purpose: CI runs `uvx pytest`, whose
        environment holds pytest and nothing else, so a third-party import here
        fails on the runner while passing locally.
        """
        ci = self._ci()
        used = re.findall(r"^\s*-?\s*uses:\s*(\S+)", ci, re.M)
        assert not [u for u in used if "setup-uv" in u], (
            "the action was only fetching a binary; a run: step does the same "
            "with one less moving part"
        )
        assert "astral.sh/uv/" in ci

    def test_the_uv_version_is_pinned(self):
        ci = self._ci()
        assert re.search(r'UV_VERSION:\s*"\d+\.\d+\.\d+"', ci), (
            "an unpinned uv lets any upstream release turn main red with no "
            "code change here -- the same rule ruff is pinned under"
        )
        assert "https://astral.sh/uv/${UV_VERSION}/install.sh" in ci
