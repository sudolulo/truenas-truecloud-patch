"""Tests for the barrier: a stable release must have been a release candidate.

This exists because the repo cut twelve releases in one day, several of them
fixing the release before -- and with the update alert live, every one of those
interrupts every user. The gate makes that path impossible rather than impolite.

The provenance gate is the one thing here that can wrongly PASS in a way nobody
notices (a wrongly-failing gate is loud; a wrongly-passing gate silently restores
the old behaviour), so it gets tested against real git repositories, not mocks.
"""

import os
import subprocess
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from release_gate import (  # noqa: E402
    check_promotable,
    next_rc,
    rc_tags,
)
from release_notes import (  # noqa: E402
    base_version,
    is_prerelease,
    promote,
    unreleased_body,
)


# ── a real git repo, because the gate reads real tags ────────────────────────

class Repo:
    """A throwaway git repo. The gate reads real tags, so the tests build real ones."""

    def __init__(self, path):
        self.path = path

    def __str__(self):
        return str(self.path)

    def git(self, *args):
        return subprocess.run(
            ["git", *args], cwd=self.path, capture_output=True, text=True, check=True,
        ).stdout.strip()

    def commit(self, msg):
        (self.path / "f").write_text(msg)
        self.git("add", "-A")
        self.git("commit", "-q", "-m", msg)
        return self.git("rev-parse", "HEAD")


@pytest.fixture
def repo(tmp_path):
    d = tmp_path / "repo"
    d.mkdir()
    r = Repo(d)
    r.git("init", "-q", "-b", "main")
    r.git("config", "user.email", "t@example.com")
    r.git("config", "user.name", "t")
    r.commit("initial")
    return r


class TestTheBarrier:
    def test_a_tag_with_no_candidate_is_refused(self, repo):
        repo.git("tag", "v1.0.0")
        problems = check_promotable("v1.0.0", cwd=str(repo))
        assert problems
        assert "never a release candidate" in problems[0]

    def test_a_tag_whose_candidate_is_on_the_same_commit_is_allowed(self, repo):
        repo.git("tag", "v1.0.0-rc1")
        repo.git("tag", "v1.0.0")
        assert check_promotable("v1.0.0", cwd=str(repo)) == []

    def test_one_more_little_fix_after_the_rc_is_refused(self, repo):
        # THE case this whole mechanism exists for. The candidate passed, then a
        # "trivial" commit landed, and the stable tag ships code no candidate ever
        # tested. That is how v0.5.1 happened.
        repo.git("tag", "v1.0.0-rc1")
        repo.commit("just a tiny fix, surely fine")
        repo.git("tag", "v1.0.0")

        problems = check_promotable("v1.0.0", cwd=str(repo))
        assert problems
        assert "no release candidate does" in problems[0]
        assert "v1.0.0-rc2" in problems[0], "must say how to fix it"

    def test_an_rc_for_a_different_version_does_not_count(self, repo):
        repo.git("tag", "v0.9.0-rc1")
        repo.git("tag", "v1.0.0")
        problems = check_promotable("v1.0.0", cwd=str(repo))
        assert problems
        assert "never a release candidate" in problems[0]

    def test_candidates_themselves_are_never_gated(self, repo):
        # Requiring an rc to have an rc would be a deadlock.
        repo.git("tag", "v1.0.0-rc1")
        assert check_promotable("v1.0.0-rc1", cwd=str(repo)) == []

    def test_a_later_candidate_on_the_right_commit_rescues_it(self, repo):
        repo.git("tag", "v1.0.0-rc1")
        repo.commit("fix found during rc1")
        repo.git("tag", "v1.0.0-rc2")   # re-cut on the fixed commit
        repo.git("tag", "v1.0.0")
        assert check_promotable("v1.0.0", cwd=str(repo)) == []

    def test_missing_tag_is_reported_not_crashed(self, repo):
        problems = check_promotable("v9.9.9", cwd=str(repo))
        assert problems and "does not exist" in problems[0]


class TestRcNumbering:
    def test_first_candidate_is_rc1(self, repo):
        assert next_rc("1.0.0", cwd=str(repo)) == "v1.0.0-rc1"

    def test_it_counts_up(self, repo):
        repo.git("tag", "v1.0.0-rc1")
        assert next_rc("1.0.0", cwd=str(repo)) == "v1.0.0-rc2"
        repo.git("tag", "v1.0.0-rc2")
        assert next_rc("1.0.0", cwd=str(repo)) == "v1.0.0-rc3"

    def test_rc10_sorts_after_rc9_not_before(self, repo):
        # Lexicographic sorting would rank rc10 before rc9 and hand out a duplicate.
        for n in range(1, 11):
            repo.git("tag", f"v1.0.0-rc{n}")
        assert rc_tags("1.0.0", cwd=str(repo))[-1] == "v1.0.0-rc10"
        assert next_rc("1.0.0", cwd=str(repo)) == "v1.0.0-rc11"

    def test_other_versions_do_not_leak_in(self, repo):
        repo.git("tag", "v0.9.0-rc7")
        assert next_rc("1.0.0", cwd=str(repo)) == "v1.0.0-rc1"


# ── the content half of the gate ─────────────────────────────────────────────

class TestPrereleaseDetection:
    @pytest.mark.parametrize("tag", ["v1.2.3-rc1", "v1.2.3-rc10", "1.2.3-beta",
                                     "v1.2.3-alpha2", "V1.2.3-RC1"])
    def test_prereleases(self, tag):
        assert is_prerelease(tag)

    @pytest.mark.parametrize("tag", ["v1.2.3", "1.2.3", "v0.0.1"])
    def test_stable(self, tag):
        assert not is_prerelease(tag)

    def test_base_version_strips_the_suffix(self):
        assert base_version("v1.2.3-rc4") == "1.2.3"
        assert base_version("v1.2.3") == "1.2.3"


class TestUnreleasedSection:
    def test_body_is_extracted(self):
        text = "# C\n\n## Unreleased\n\n### Fixed\n- a thing\n\n## v1.0.0 — 2026-01-01\n\n- old\n"
        assert "- a thing" in unreleased_body(text)
        assert "old" not in unreleased_body(text)

    def test_empty_section_reads_as_empty(self):
        text = "# C\n\n## Unreleased\n\n## v1.0.0 — 2026-01-01\n\n- old\n"
        assert unreleased_body(text) == ""

    def test_absent_section_reads_as_empty(self):
        assert unreleased_body("# C\n\n## v1.0.0 — 2026-01-01\n\n- old\n") == ""

    def test_promote_renames_the_heading_and_keeps_the_body(self):
        text = "# C\n\n## Unreleased\n\n### Fixed\n- a thing\n\n## v1.0.0 — 2026-01-01\n"
        out = promote(text, "1.1.0", "2026-07-13")
        assert "## v1.1.0 — 2026-07-13" in out
        assert "## Unreleased" not in out
        assert "- a thing" in out
        assert "## v1.0.0 — 2026-01-01" in out, "older sections survive"

    def test_promoting_nothing_is_refused(self):
        # A release with no content is a release nobody needed -- and it still
        # alerts every box.
        with pytest.raises(ValueError, match="nothing to release"):
            promote("# C\n\n## v1.0.0 — 2026-01-01\n", "1.1.0", "2026-07-13")


class TestStrandedWorkBlocksAStableRelease:
    """`check()` refuses a stable tag that leaves work under `## Unreleased`.

    Either it is finished and belongs in the release, or the release is premature.
    """

    def _tree(self, tmp_path, changelog):
        from release_notes import VERSIONED_FILES
        for rel in VERSIONED_FILES:
            p = tmp_path / rel
            p.parent.mkdir(parents=True, exist_ok=True)
            marker = "__version__ = " if rel.endswith(".py") else "VERSION="
            p.write_text(f'{marker}"1.0.0"\n')
        (tmp_path / "CHANGELOG.md").write_text(changelog)
        return str(tmp_path)

    def test_stranded_work_is_refused_for_a_stable_tag(self, tmp_path):
        from release_notes import check
        root = self._tree(tmp_path, (
            "# C\n\n## Unreleased\n\n### Fixed\n- not done yet\n\n"
            "## v1.0.0 — 2026-07-13\n\n### Added\n- the thing\n"
        ))
        problems = check("v1.0.0", root=root)
        assert any("Unreleased" in p for p in problems)

    def test_stranded_work_is_fine_for_a_candidate(self, tmp_path):
        # An rc may legitimately have more work queued behind it.
        from release_notes import check
        root = self._tree(tmp_path, (
            "# C\n\n## Unreleased\n\n### Fixed\n- later\n\n"
            "## v1.0.0 — 2026-07-13\n\n### Added\n- the thing\n"
        ))
        assert check("v1.0.0-rc1", root=root) == []

    def test_a_clean_stable_release_passes(self, tmp_path):
        from release_notes import check
        root = self._tree(tmp_path, (
            "# C\n\n## v1.0.0 — 2026-07-13\n\n### Added\n- the thing\n"
        ))
        assert check("v1.0.0", root=root) == []

    def test_a_candidate_checks_against_its_base_version(self, tmp_path):
        # The scripts say 1.0.0; the tag says v1.0.0-rc3. That must agree, not clash.
        from release_notes import check
        root = self._tree(tmp_path, (
            "# C\n\n## v1.0.0 — 2026-07-13\n\n### Added\n- the thing\n"
        ))
        assert check("v1.0.0-rc3", root=root) == []
