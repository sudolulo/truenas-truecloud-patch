"""Tests for nested-dataset snapshot staging.

The cardinal rule under test: a tree that cannot be staged completely must fail
LOUDLY. A silently-incomplete backup is the exact failure that stock TrueNAS's
"no further nesting" guard exists to prevent, and it is the one regression this
feature must never introduce.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "patch"))

from truecloud_nested import (  # noqa: E402
    StagingError,
    apply_plan,
    current_mounts_under,
    plan_staging,
    staging_root_for,
    teardown,
    verify_staged,
)

SNAP = "cloud_backup-5-20260712030000"
ROOT = "/run/truecloud-nested/cloud_backup-5"


def ds(name, mountpoint, mounted="yes"):
    return {
        "name": name,
        "properties": {
            "mountpoint": {"value": mountpoint},
            "mounted": {"value": mounted},
        },
    }


# Mirrors the real layout: apps are datasets, several with their own children.
DATASETS = [
    ds("Tap", "/mnt/Tap"),
    ds("Tap/apps", "/mnt/Tap/apps"),
    ds("Tap/apps/lidarr", "/mnt/Tap/apps/lidarr"),
    ds("Tap/apps/lidarr/config", "/mnt/Tap/apps/lidarr/config"),
    ds("Tap/apps/immich", "/mnt/Tap/apps/immich"),
    ds("Tap/apps/immich/pgdata", "/mnt/Tap/apps/immich/pgdata"),
]

ALL_DIRS_EXIST = lambda _p: True  # noqa: E731


class TestPlanStaging:
    def test_stages_every_descendant_dataset(self):
        mounts, skipped = plan_staging(
            "/mnt/Tap", "/mnt/Tap", SNAP, DATASETS, ROOT, isdir=ALL_DIRS_EXIST
        )
        assert skipped == []

        # Root + all 5 descendants. The base dataset itself is the root, not a
        # descendant, so it must not be double-mounted.
        assert len(mounts) == 6
        assert mounts[0] == (f"/mnt/Tap/.zfs/snapshot/{SNAP}", ROOT)

        by_target = dict((t, s) for s, t in mounts)
        assert by_target[f"{ROOT}/apps"] == f"/mnt/Tap/apps/.zfs/snapshot/{SNAP}"
        assert by_target[f"{ROOT}/apps/immich/pgdata"] == (
            f"/mnt/Tap/apps/immich/pgdata/.zfs/snapshot/{SNAP}"
        )

    def test_parents_are_mounted_before_children(self):
        # A child's mountpoint dir only exists inside its parent's snapshot, so
        # mounting a child first would fail.
        mounts, _ = plan_staging(
            "/mnt/Tap", "/mnt/Tap", SNAP, DATASETS, ROOT, isdir=ALL_DIRS_EXIST
        )
        seen = set()
        for _src, target in mounts:
            parent = os.path.dirname(target)
            if target != ROOT:
                assert parent in seen or parent == ROOT, f"{target} mounted before {parent}"
            seen.add(target)

    def test_backup_path_below_dataset_root(self):
        mounts, _ = plan_staging(
            "/mnt/Tap", "/mnt/Tap/apps", SNAP, DATASETS, ROOT, isdir=ALL_DIRS_EXIST
        )
        # Root source is the *subdirectory* inside the base dataset's snapshot.
        assert mounts[0] == (f"/mnt/Tap/.zfs/snapshot/{SNAP}/apps", ROOT)
        targets = [t for _s, t in mounts]
        assert f"{ROOT}/lidarr" in targets       # relative to /mnt/Tap/apps
        assert f"{ROOT}/apps/lidarr" not in targets

    def test_base_dataset_is_not_a_descendant_of_itself(self):
        mounts, _ = plan_staging(
            "/mnt/Tap", "/mnt/Tap", SNAP, [ds("Tap", "/mnt/Tap")], ROOT, isdir=ALL_DIRS_EXIST
        )
        assert len(mounts) == 1  # just the root


class TestSkipping:
    @pytest.mark.parametrize("mp", ["none", "legacy", "-", ""])
    def test_unmountable_mountpoints_are_skipped_and_reported(self, mp):
        datasets = DATASETS + [ds("Tap/weird", mp)]
        mounts, skipped = plan_staging(
            "/mnt/Tap", "/mnt/Tap", SNAP, datasets, ROOT, isdir=ALL_DIRS_EXIST
        )
        assert len(mounts) == 6
        assert any(name == "Tap/weird" for name, _reason in skipped)

    def test_unmounted_dataset_is_skipped_but_never_silently(self):
        # A locked/encrypted dataset contributes nothing to the live tree either,
        # so skipping matches stock semantics -- but it MUST be reported.
        datasets = DATASETS + [ds("Tap/apps/vault", "/mnt/Tap/apps/vault", mounted="no")]
        mounts, skipped = plan_staging(
            "/mnt/Tap", "/mnt/Tap", SNAP, datasets, ROOT, isdir=ALL_DIRS_EXIST
        )
        assert f"{ROOT}/apps/vault" not in [t for _s, t in mounts]
        assert ("Tap/apps/vault", "dataset is not mounted (locked/encrypted?)") in skipped


class TestSilentOmissionGuard:
    """The whole point of the feature. These are the tests that matter."""

    def test_missing_snapshot_on_descendant_raises(self):
        # If the recursive snapshot somehow missed a dataset, staging it would
        # silently omit its data. Refuse rather than upload an incomplete tree.
        def isdir(path):
            return "/mnt/Tap/apps/immich/pgdata/" not in path

        with pytest.raises(StagingError, match="incomplete tree"):
            plan_staging("/mnt/Tap", "/mnt/Tap", SNAP, DATASETS, ROOT, isdir=isdir)

    def test_error_names_the_offending_dataset(self):
        def isdir(path):
            return "/mnt/Tap/apps/immich/pgdata/" not in path

        with pytest.raises(StagingError, match="Tap/apps/immich/pgdata"):
            plan_staging("/mnt/Tap", "/mnt/Tap", SNAP, DATASETS, ROOT, isdir=isdir)


class TestVerifyStaged:
    """Anti-regression guard: proves the staged tree is real before we back it up."""

    def test_passes_when_every_target_is_mounted_and_root_non_empty(self):
        mounts = [("/src", ROOT), ("/src/a", f"{ROOT}/a")]
        assert verify_staged(
            mounts, ismount=lambda p: True, listdir=lambda p: ["apps"]
        )

    def test_raises_when_a_target_is_not_actually_mounted(self):
        # This is the case that would produce a silently-empty backup.
        mounts = [("/src", ROOT), ("/src/a", f"{ROOT}/a")]
        with pytest.raises(StagingError, match="not a mountpoint"):
            verify_staged(
                mounts, ismount=lambda p: p == ROOT, listdir=lambda p: ["apps"]
            )

    def test_raises_when_staging_root_is_empty(self):
        mounts = [("/src", ROOT)]
        with pytest.raises(StagingError, match="empty"):
            verify_staged(mounts, ismount=lambda p: True, listdir=lambda p: [])

    def test_raises_on_empty_plan(self):
        with pytest.raises(StagingError):
            verify_staged([])


class FakeRunner:
    def __init__(self, fail_on=None):
        self.fail_on = fail_on
        self.calls = []

    def __call__(self, cmd):
        self.calls.append(cmd)

        class R:
            returncode = 0
            stderr = ""

        if self.fail_on and cmd[0] == "mount" and cmd[2] == self.fail_on:
            R.returncode = 32
            R.stderr = "mount failed"
        return R


class TestApplyPlanRollback:
    def test_rolls_back_mounts_when_one_fails(self, tmp_path, monkeypatch):
        # A half-built tree must never reach the backup tool.
        root = str(tmp_path / "root")
        mounts = [("/src", root), ("/src/a", root + "/a"), ("/src/b", root + "/b")]
        monkeypatch.setattr(os.path, "isdir", lambda p: True)

        runner = FakeRunner(fail_on="/src/b")
        with pytest.raises(StagingError, match="bind-mount"):
            apply_plan(mounts, runner=runner)

        umounts = [c for c in runner.calls if c[0] == "umount"]
        # Everything successfully mounted before the failure is unmounted again.
        assert [c[-1] for c in umounts] == [root + "/a", root]

    def test_raises_when_target_missing(self, tmp_path, monkeypatch):
        root = str(tmp_path / "root")
        monkeypatch.setattr(os.path, "isdir", lambda p: p == root)
        with pytest.raises(StagingError, match="does not exist"):
            apply_plan([("/src", root), ("/src/a", root + "/a")], runner=FakeRunner())


class TestTeardown:
    def test_unmounts_deepest_first(self, tmp_path):
        mounts_file = tmp_path / "mounts"
        mounts_file.write_text(
            f"tmpfs {ROOT} tmpfs rw 0 0\n"
            f"tmpfs {ROOT}/apps tmpfs rw 0 0\n"
            f"tmpfs {ROOT}/apps/lidarr/config tmpfs rw 0 0\n"
            f"tmpfs {ROOT}/apps/lidarr tmpfs rw 0 0\n"
            "tmpfs /somewhere/else tmpfs rw 0 0\n"
        )
        runner = FakeRunner()
        teardown(ROOT, runner=runner, mounts_file=str(mounts_file))

        order = [c[-1] for c in runner.calls if c[0] == "umount"]
        assert order == [
            f"{ROOT}/apps/lidarr/config",
            f"{ROOT}/apps/lidarr",
            f"{ROOT}/apps",
            ROOT,
        ]
        assert "/somewhere/else" not in order  # never touch unrelated mounts

    def test_is_idempotent_when_nothing_mounted(self, tmp_path):
        mounts_file = tmp_path / "mounts"
        mounts_file.write_text("tmpfs /somewhere/else tmpfs rw 0 0\n")
        runner = FakeRunner()
        assert teardown(ROOT, runner=runner, mounts_file=str(mounts_file)) == []
        assert runner.calls == []

    def test_falls_back_to_lazy_umount(self, tmp_path):
        mounts_file = tmp_path / "mounts"
        mounts_file.write_text(f"tmpfs {ROOT} tmpfs rw 0 0\n")

        class Busy(FakeRunner):
            def __call__(self, cmd):
                self.calls.append(cmd)

                class R:
                    returncode = 0 if "-l" in cmd else 32
                    stderr = "target is busy"

                return R

        runner = Busy()
        assert teardown(ROOT, runner=runner, mounts_file=str(mounts_file)) == []
        assert ["umount", "-l", ROOT] in runner.calls


class TestCurrentMountsUnder:
    def test_matches_only_the_staging_subtree(self, tmp_path):
        mounts_file = tmp_path / "mounts"
        # "/run/truecloud-nested/cloud_backup-50" must NOT match "cloud_backup-5".
        mounts_file.write_text(
            f"tmpfs {ROOT} tmpfs rw 0 0\n"
            "tmpfs /run/truecloud-nested/cloud_backup-50 tmpfs rw 0 0\n"
        )
        found = current_mounts_under(ROOT, mounts_file=str(mounts_file))
        assert found == [ROOT]


class TestStagingRootFor:
    def test_stable_per_task(self):
        assert staging_root_for("cloud_backup-5") == "/run/truecloud-nested/cloud_backup-5"

    def test_sanitises_path_separators(self):
        assert "/" not in staging_root_for("evil/../../etc").rsplit("/", 1)[-1]
