"""Tests for the release automation.

The release workflow refuses to publish unless these hold, so a bad tag fails
loudly in CI instead of shipping a release whose notes are empty, wrong, or whose
scripts announce a different version than the tag.

That last one is not hypothetical: VERSION= drifted to three different values
across install.sh / uninstall.sh / recover.sh / apply.sh and nothing noticed.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))

from release_notes import (  # noqa: E402
    changelog_versions,
    check,
    extract_notes,
    normalise,
    script_versions,
    significance,
    version_tuple,
)

REPO = os.path.join(os.path.dirname(__file__), "..")

SAMPLE = """\
# Changelog

## v0.3.0 — 2026-07-13

### Added

- the new thing

## v0.2.1 — 2026-07-09

### Fixed

- the old thing

## v0.2.0 — 2026-07-08

- first
"""


class TestExtractNotes:
    def test_returns_only_that_versions_body(self):
        body = extract_notes(SAMPLE, "v0.3.0")
        assert "the new thing" in body
        assert "the old thing" not in body
        # The version heading itself is dropped (GitHub renders its own title),
        # but sub-headings like "### Added" must survive.
        assert not body.startswith("## v")
        assert body.startswith("### Added")

    def test_stops_at_the_next_version_heading(self):
        body = extract_notes(SAMPLE, "v0.2.1")
        assert "the old thing" in body
        assert "first" not in body

    def test_last_section_runs_to_end_of_file(self):
        assert "first" in extract_notes(SAMPLE, "v0.2.0")

    def test_accepts_the_tag_with_or_without_the_v(self):
        assert extract_notes(SAMPLE, "0.3.0") == extract_notes(SAMPLE, "v0.3.0")

    def test_unknown_version_raises_rather_than_returning_empty(self):
        # An empty release body is worse than a failed release.
        with pytest.raises(KeyError, match="no section"):
            extract_notes(SAMPLE, "v9.9.9")


class TestChangelogVersions:
    def test_lists_versions_newest_first(self):
        assert changelog_versions(SAMPLE) == ["0.3.0", "0.2.1", "0.2.0"]


class TestAgainstTheRealRepo:
    """These run against the actual files, so drift breaks the build."""

    def test_every_script_declares_a_version(self):
        from release_notes import VERSIONED_FILES

        found = script_versions(REPO)
        missing = [f for f in VERSIONED_FILES if f not in found]
        assert not missing, f"no VERSION= in: {missing}"

    def test_all_scripts_agree_on_the_version(self):
        versions = {normalise(v) for v in script_versions(REPO).values()}
        assert len(versions) == 1, f"scripts disagree on version: {sorted(versions)}"

    def test_the_current_version_has_a_changelog_section(self):
        version = next(iter({normalise(v) for v in script_versions(REPO).values()}))
        with open(os.path.join(REPO, "CHANGELOG.md"), encoding="utf-8") as fh:
            body = extract_notes(fh.read(), version)
        assert body, f"CHANGELOG.md has no content for v{version}"

    def test_the_current_version_is_the_newest_changelog_entry(self):
        version = next(iter({normalise(v) for v in script_versions(REPO).values()}))
        with open(os.path.join(REPO, "CHANGELOG.md"), encoding="utf-8") as fh:
            newest = changelog_versions(fh.read())[0]
        assert newest == version, (
            f"scripts say v{version} but the newest CHANGELOG entry is v{newest}"
        )

    def test_check_passes_for_the_current_version(self):
        version = next(iter({normalise(v) for v in script_versions(REPO).values()}))
        assert check(version, REPO) == []


class TestCheckCatchesMistakes:
    def test_reports_a_tag_that_no_script_matches(self):
        problems = check("v9.9.9", REPO)
        assert problems
        assert any("declares VERSION" in p for p in problems)

    def test_reports_a_missing_changelog_section(self):
        problems = check("v9.9.9", REPO)
        assert any("no section" in p for p in problems)


class TestSignificance:
    """Drives the TrueNAS update alert: what is worth bothering a human about.

    The rule: a release whose CHANGELOG only has a "### Docs" section changed no
    code, and nobody should get an alert because a README was reworded.
    """

    TEXT = """\
# Changelog

## v0.4.2 — 2026-07-13

### Docs

- reworded the README

## v0.4.1 — 2026-07-13

### Fixed

- a real bug

## v0.4.0 — 2026-07-13

### Added

- a feature

## v0.3.3 — 2026-07-13

### Security

- keep a password out of argv

## v0.3.2 — 2026-07-13

### Fixed

- something
"""

    def test_docs_only_release_does_not_alert(self):
        level, versions, _ = significance(self.TEXT, "0.4.1", "0.4.2")
        assert level == "docs"
        assert versions == ["0.4.2"]

    def test_a_real_fix_alerts(self):
        level, _v, _h = significance(self.TEXT, "0.4.0", "0.4.1")
        assert level == "notable"

    def test_security_in_range_escalates(self):
        level, _v, _h = significance(self.TEXT, "0.3.2", "0.3.3")
        assert level == "security"

    def test_security_wins_even_when_the_newest_release_is_docs_only(self):
        # A docs-only v0.4.2 sitting on top of a security-fixing v0.3.3 must still
        # be reported as security — classify the whole span, not just the tip.
        level, versions, _ = significance(self.TEXT, "0.3.2", "0.4.2")
        assert level == "security"
        assert set(versions) == {"0.3.3", "0.4.0", "0.4.1", "0.4.2"}

    def test_same_version_is_never_notable(self):
        level, versions, _ = significance(self.TEXT, "0.4.2", "0.4.2")
        assert level == "docs"
        assert versions == []

    def test_range_is_exclusive_of_current_inclusive_of_latest(self):
        _l, versions, _h = significance(self.TEXT, "0.4.0", "0.4.2")
        assert "0.4.0" not in versions
        assert "0.4.2" in versions

    def test_version_tuple_orders_correctly(self):
        assert version_tuple("v0.10.0") > version_tuple("v0.9.9")
        assert version_tuple("0.4.2") > version_tuple("0.4.1")
        # Pre-release suffixes are dropped, not ranked above the release.
        assert version_tuple("v0.5.0-rc1") == version_tuple("v0.5.0")
