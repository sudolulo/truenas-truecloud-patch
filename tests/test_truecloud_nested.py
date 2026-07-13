"""Tests for nested-dataset snapshot staging.

Two rules are under test above all else:

1. A tree that cannot be staged completely must fail LOUDLY. A silently
   incomplete backup is the exact failure that stock TrueNAS's "no further
   nesting" guard exists to prevent.

2. Every snapshot we cause to exist must be cleaned up. ``zfs.snapshot.delete``
   is non-recursive by default and stock calls it with no options, so a
   recursive snapshot would otherwise orphan one snapshot per descendant dataset
   on EVERY run.
"""

import os
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "patch"))

from truecloud_nested import (  # noqa: E402
    StagingError,
    apply_plan,
    cleanup_all,
    cleanup_task,
    current_mounts_under,
    delete_snapshot_tree,
    plan_staging,
    sidecar_for,
    snapshot_tree_names,
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


def yes(_path):
    return "ok"


def plan(datasets=DATASETS, base_dataset="Tap", base_mp="/mnt/Tap",
         path="/mnt/Tap", probe=yes):
    return plan_staging(base_dataset, base_mp, path, SNAP, datasets, ROOT, probe=probe)


class TestPlanStaging:
    def test_stages_every_descendant_dataset(self):
        mounts, skipped = plan()
        assert skipped == []
        assert len(mounts) == 6  # root + 5 descendants
        assert mounts[0] == (f"/mnt/Tap/.zfs/snapshot/{SNAP}", ROOT)

        by_target = {t: s for s, t in mounts}
        assert by_target[f"{ROOT}/apps"] == f"/mnt/Tap/apps/.zfs/snapshot/{SNAP}"
        assert by_target[f"{ROOT}/apps/immich/pgdata"] == (
            f"/mnt/Tap/apps/immich/pgdata/.zfs/snapshot/{SNAP}"
        )

    def test_parents_are_mounted_before_children(self):
        # A child's mountpoint dir only exists inside its parent's snapshot, so
        # mounting a child first would fail.
        mounts, _ = plan()
        seen = set()
        for _src, target in mounts:
            if target != ROOT:
                assert os.path.dirname(target) in seen
            seen.add(target)

    def test_backup_path_below_dataset_root(self):
        mounts, _ = plan(base_dataset="Tap/apps", base_mp="/mnt/Tap/apps",
                         path="/mnt/Tap/apps")
        assert mounts[0] == (f"/mnt/Tap/apps/.zfs/snapshot/{SNAP}", ROOT)
        targets = [t for _s, t in mounts]
        assert f"{ROOT}/lidarr" in targets
        assert f"{ROOT}/apps/lidarr" not in targets

    def test_base_dataset_is_not_a_descendant_of_itself(self):
        mounts, _ = plan(datasets=[ds("Tap", "/mnt/Tap")])
        assert len(mounts) == 1


class TestScoping:
    def test_unrelated_datasets_are_ignored_silently(self):
        # Regression: scoping by mountpoint first dragged in every
        # mountpoint-less dataset on the box (all of Tank/.system/*), burying the
        # warnings that actually matter.
        noisy = DATASETS + [
            ds("Tank/.system", "none"),
            ds("Tank/.system/cores", "legacy"),
            ds("Tank/backups", "/mnt/Tank/backups"),
        ]
        mounts, skipped = plan(datasets=noisy)
        assert len(mounts) == 6
        assert skipped == [], "datasets outside the base dataset must not be reported"

    def test_in_scope_dataset_without_mountpoint_is_reported(self):
        datasets = DATASETS + [ds("Tap/apps/weird", "none")]
        _mounts, skipped = plan(datasets=datasets)
        assert ("Tap/apps/weird", "mountpoint is none") in skipped

    def test_unmounted_dataset_is_skipped_but_never_silently(self):
        datasets = DATASETS + [ds("Tap/apps/vault", "/mnt/Tap/apps/vault", mounted="no")]
        mounts, skipped = plan(datasets=datasets)
        assert f"{ROOT}/apps/vault" not in [t for _s, t in mounts]
        assert ("Tap/apps/vault", "dataset is not mounted (locked/encrypted?)") in skipped

    def test_descendant_mounted_outside_the_path_is_not_an_omission(self):
        datasets = DATASETS + [ds("Tap/elsewhere", "/mnt/other")]
        mounts, skipped = plan(datasets=datasets)
        assert len(mounts) == 6
        assert skipped == []


class TestSilentOmissionGuard:
    """The whole point of the feature. These are the tests that matter."""

    @staticmethod
    def _missing_pgdata(path):
        return "missing" if "/mnt/Tap/apps/immich/pgdata/" in path else "ok"

    @staticmethod
    def _denied_pgdata(path):
        if "/mnt/Tap/apps/immich/pgdata/" in path:
            return "cannot be read (Permission denied)"
        return "ok"

    def test_missing_snapshot_on_descendant_raises(self):
        with pytest.raises(StagingError, match="incomplete tree"):
            plan(probe=self._missing_pgdata)

    def test_error_names_the_offending_dataset(self):
        with pytest.raises(StagingError, match="Tap/apps/immich/pgdata"):
            plan(probe=self._missing_pgdata)

    def test_missing_and_unreadable_are_reported_differently(self):
        # os.path.isdir() collapses both into False, which would report a
        # permission problem as "has no snapshot" and send you hunting for a
        # snapshot that is sitting right there. Both abort -- but say which.
        with pytest.raises(StagingError, match="has no snapshot"):
            plan(probe=self._missing_pgdata)
        with pytest.raises(StagingError, match="Permission denied"):
            plan(probe=self._denied_pgdata)


class TestSnapshotTreeNames:
    """zfs.snapshot.delete is non-recursive; we must sweep children ourselves."""

    ALL = [
        "Tap@cloud_backup-5-20260712030000",
        "Tap/apps@cloud_backup-5-20260712030000",
        "Tap/apps/lidarr/config@cloud_backup-5-20260712030000",
        "Tap@auto-2026-07-12_03-00",            # unrelated periodic snapshot
        "Tap/apps@cloud_backup-9-20260712030000",  # another task
        "Tank/backups@cloud_backup-5-20260712030000",  # different pool
    ]

    def test_returns_parent_and_all_children(self):
        got = snapshot_tree_names("Tap@cloud_backup-5-20260712030000", self.ALL)
        assert set(got) == {
            "Tap@cloud_backup-5-20260712030000",
            "Tap/apps@cloud_backup-5-20260712030000",
            "Tap/apps/lidarr/config@cloud_backup-5-20260712030000",
        }

    def test_never_touches_periodic_or_other_tasks_or_other_pools(self):
        got = snapshot_tree_names("Tap@cloud_backup-5-20260712030000", self.ALL)
        assert "Tap@auto-2026-07-12_03-00" not in got
        assert "Tap/apps@cloud_backup-9-20260712030000" not in got
        assert "Tank/backups@cloud_backup-5-20260712030000" not in got

    def test_malformed_snapshot_name_yields_nothing(self):
        assert snapshot_tree_names("Tap", self.ALL) == []


class FakeMiddleware:
    """middlewared as this module actually uses it: `call_sync`, from a thread.

    The module is synchronous on purpose -- see the orchestration note in
    truecloud_nested.py. TrueNAS <= 25.10 reaches it through
    `await middleware.run_in_thread(...)` and TrueNAS 26 calls it directly, but the
    logic below the boundary is the same code either way, so it is tested once.
    """

    def __init__(self, snapshots=None):
        self.snapshots = list(snapshots or [])
        self.calls = []
        self.logger = None

    def call_sync(self, method, *args):
        self.calls.append((method, args))
        if method == "zfs.snapshot.query":
            return [{"name": n} for n in self.snapshots]
        if method == "zfs.snapshot.delete":
            name = args[0]
            opts = args[1] if len(args) > 1 else {}
            if name not in self.snapshots:
                raise RuntimeError("does not exist")
            if opts.get("recursive"):
                # Real `zfs destroy -r` takes the parent and every child snapshot.
                for n in snapshot_tree_names(name, list(self.snapshots)):
                    self.snapshots.remove(n)
            else:
                self.snapshots.remove(name)
            return True
        raise AssertionError(f"unexpected call {method}")


def stub_core(monkeypatch, tn, *, plan=None, order=None, plan_raises=None):
    """Replace the blocking core (plan/apply/verify/teardown) with recorders.

    stage_nested calls these directly now, so they are patched by NAME rather than
    intercepted at a `run_in_thread` boundary that no longer exists.
    """
    def record(name, result):
        def fn(*args, **kwargs):
            if order is not None:
                order.append(name)
            if name == "plan_staging" and plan_raises is not None:
                raise plan_raises
            return result() if callable(result) else result
        fn.__name__ = name
        return fn

    real_write = tn._write_sidecar

    def write_sidecar(*args, **kwargs):
        if order is not None:
            order.append("_write_sidecar")
        return real_write(*args, **kwargs)

    monkeypatch.setattr(tn, "_write_sidecar", write_sidecar)
    monkeypatch.setattr(tn, "plan_staging", record("plan_staging", plan or ([], [])))
    monkeypatch.setattr(tn, "apply_plan", record("apply_plan", True))
    monkeypatch.setattr(tn, "verify_staged", record("verify_staged", True))
    monkeypatch.setattr(tn, "teardown", record("teardown", []))


class TestDeleteSnapshotTree:
    def test_deletes_parent_and_every_child(self):
        mw = FakeMiddleware([
            "Tap@snap", "Tap/apps@snap", "Tap/apps/lidarr@snap", "Tap@keepme",
        ])
        delete_snapshot_tree(mw, "Tap@snap")
        assert mw.snapshots == ["Tap@keepme"]

    def test_is_idempotent_when_stock_already_removed_the_parent(self):
        # Stock's finally can win the race once our mounts are released.
        mw = FakeMiddleware(["Tap/apps@snap", "Tap/apps/lidarr@snap"])
        delete_snapshot_tree(mw, "Tap@snap")
        assert mw.snapshots == []

    def test_uses_a_single_recursive_delete_not_252_individual_ones(self):
        # 252 sequential deletes are slow AND not atomic: a run killed part-way
        # through leaves exactly the orphans this function exists to prevent.
        mw = FakeMiddleware(["Tap@snap", "Tap/apps@snap", "Tap/apps/lidarr@snap"])
        delete_snapshot_tree(mw, "Tap@snap")
        assert mw.snapshots == []
        deletes = [a for m, a in mw.calls if m == "zfs.snapshot.delete"]
        assert len(deletes) == 1, "should be ONE recursive call, not one per snapshot"
        assert deletes[0][1] == {"recursive": True}
        assert not [m for m, _a in mw.calls if m == "zfs.snapshot.query"], (
            "no enumeration needed on the fast path"
        )

    def test_survives_recursive_and_query_failure_by_deleting_the_parent(self):
        class Broken(FakeMiddleware):
            def call_sync(self, method, *args):
                if method == "zfs.snapshot.query":
                    raise RuntimeError("boom")
                if method == "zfs.snapshot.delete" and len(args) > 1:
                    raise RuntimeError("recursive delete unavailable")
                return super().call_sync(method, *args)

        mw = Broken(["Tap@snap"])
        delete_snapshot_tree(mw, "Tap@snap")
        assert mw.snapshots == []

    def test_leaves_unrelated_snapshots_alone_when_the_tree_is_gone(self):
        mw = FakeMiddleware(["Tap@unrelated"])
        delete_snapshot_tree(mw, "Tap@snap")
        assert mw.snapshots == ["Tap@unrelated"]


class TestStageNestedOrdering:
    def test_sidecar_is_written_before_anything_is_mounted(self, tmp_path, monkeypatch):
        # middlewared can die at any moment. If the snapshot were recorded only
        # after apply_plan, a crash in that window would orphan a 160-snapshot
        # tree -- the precise failure the sidecar exists to prevent.
        import truecloud_nested as tn

        monkeypatch.setattr(tn, "STAGING_BASE", str(tmp_path))
        order = []
        stub_core(monkeypatch, tn, order=order,
                  plan=([("/src", str(tmp_path / "cloud_backup-5"))], []))

        tn.stage_nested(
            FakeMiddleware(), "/mnt/Tap", "Tap@snap", "Tap", "/mnt/Tap",
            "cloud_backup-5", DATASETS,
        )

        assert order.index("_write_sidecar") < order.index("apply_plan")

    def test_reclaims_the_snapshot_tree_left_by_a_crashed_run(self, tmp_path,
                                                              monkeypatch):
        # teardown() reclaims the crashed run's MOUNTS, but nothing else would
        # ever reclaim its SNAPSHOTS -- and we are about to overwrite the only
        # record of them. One crash would orphan 160+ snapshots permanently.
        import truecloud_nested as tn

        monkeypatch.setattr(tn, "STAGING_BASE", str(tmp_path))
        root = tn.staging_root_for("cloud_backup-5")
        os.makedirs(os.path.dirname(root), exist_ok=True)
        with open(sidecar_for(root), "w", encoding="utf-8") as fh:
            fh.write("Tap@old-crashed-run")

        mw = FakeMiddleware(["Tap@old-crashed-run", "Tap/apps@old-crashed-run"])
        stub_core(monkeypatch, tn, plan=([("/src", root)], []))

        tn.stage_nested(
            mw, "/mnt/Tap", "Tap@new", "Tap", "/mnt/Tap",
            "cloud_backup-5", DATASETS,
        )

        assert mw.snapshots == [], "the crashed run's snapshot tree must be reclaimed"

    def test_sidecar_is_KEPT_when_staging_fails(self, tmp_path, monkeypatch):
        # The caller sweeps the snapshot tree on the way out, and anything still busy
        # SURVIVES that sweep -- with the sidecar as its only record. Removing the
        # sidecar here would orphan those snapshots permanently.
        #
        # The asymmetry is the point: a sidecar left behind when the tree is already
        # gone costs one no-op delete on the next run; a sidecar removed while the tree
        # still exists is unrecoverable.
        import truecloud_nested as tn

        monkeypatch.setattr(tn, "STAGING_BASE", str(tmp_path))
        root = tn.staging_root_for("cloud_backup-5")
        stub_core(monkeypatch, tn, plan_raises=StagingError("boom"))

        with pytest.raises(StagingError):
            tn.stage_nested(
                FakeMiddleware(), "/mnt/Tap", "Tap@snap", "Tap", "/mnt/Tap",
                "cloud_backup-5", DATASETS,
            )

        assert os.path.exists(sidecar_for(root)), (
            "sidecar removed on staging failure — any snapshot the caller's sweep "
            "cannot delete is now orphaned forever"
        )
        with open(sidecar_for(root), encoding="utf-8") as fh:
            assert fh.read().strip() == "Tap@snap"


class TestCleanupTask:
    def test_recovers_snapshot_from_sidecar_after_middlewared_restart(self, tmp_path,
                                                                      monkeypatch):
        # The sidecar is the ONLY record of the pinned snapshot, precisely so a
        # middlewared restart cannot orphan the tree.
        import truecloud_nested as tn

        monkeypatch.setattr(tn, "STAGING_BASE", str(tmp_path))
        root = tn.staging_root_for("cloud_backup-5", base=str(tmp_path))
        os.makedirs(root, exist_ok=True)
        with open(sidecar_for(root), "w", encoding="utf-8") as fh:
            fh.write("Tap@snap")

        mw = FakeMiddleware(["Tap@snap", "Tap/apps@snap"])
        monkeypatch.setattr(tn, "teardown", lambda *_a, **_k: [])

        cleanup_task(mw, "cloud_backup-5")

        assert mw.snapshots == []
        assert not os.path.exists(sidecar_for(root))

    def test_is_a_noop_when_never_staged(self, tmp_path, monkeypatch):
        import truecloud_nested as tn

        monkeypatch.setattr(tn, "STAGING_BASE", str(tmp_path / "nope"))
        mw = FakeMiddleware(["Tap@snap"])
        cleanup_task(mw, "cloud_backup-5")
        assert mw.calls == []
        assert mw.snapshots == ["Tap@snap"]


class TestVerifyStaged:
    """Anti-regression guard: proves the staged tree is real before we back it up."""

    def test_passes_when_every_target_is_mounted_and_root_non_empty(self):
        mounts = [("/src", ROOT), ("/src/a", f"{ROOT}/a")]
        assert verify_staged(mounts, ismount=lambda p: True, listdir=lambda p: ["apps"])

    def test_raises_when_a_target_is_not_actually_mounted(self):
        # This is the case that would produce a silently-empty backup.
        mounts = [("/src", ROOT), ("/src/a", f"{ROOT}/a")]
        with pytest.raises(StagingError, match="not a mountpoint"):
            verify_staged(mounts, ismount=lambda p: p == ROOT, listdir=lambda p: ["apps"])

    def test_raises_when_staging_root_is_empty(self):
        with pytest.raises(StagingError, match="empty"):
            verify_staged([("/src", ROOT)], ismount=lambda p: True, listdir=lambda p: [])

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
    def test_rolls_back_mounts_when_one_fails(self, tmp_path):
        # A half-built tree must never reach the backup tool.
        root = str(tmp_path / "root")
        mounts = [("/src", root), ("/src/a", root + "/a"), ("/src/b", root + "/b")]

        runner = FakeRunner(fail_on="/src/b")
        with pytest.raises(StagingError, match="bind-mount"):
            apply_plan(mounts, runner=runner, isdir=lambda _p: True)

        umounts = [c[-1] for c in runner.calls if c[0] == "umount"]
        assert umounts == [root + "/a", root]

    def test_raises_when_target_missing(self, tmp_path):
        root = str(tmp_path / "root")
        with pytest.raises(StagingError, match="does not exist"):
            apply_plan(
                [("/src", root), ("/src/a", root + "/a")],
                runner=FakeRunner(),
                isdir=lambda p: p == root,
            )


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
        assert "/somewhere/else" not in order

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


class TestCleanupAll:
    """uninstall.sh and recover.sh call this instead of reimplementing teardown."""

    def test_reports_orphan_snapshots_before_deleting_their_sidecars(self, tmp_path):
        # The sidecar is the only record that an interrupted run's snapshot tree
        # is still on disk. Deleting it without naming the snapshot orphans the
        # whole tree silently.
        base = tmp_path / "stage"
        base.mkdir()
        (base / "cloud_backup-5.snapshot").write_text("Tap@interrupted")

        mounts_file = tmp_path / "mounts"
        mounts_file.write_text("")

        lines, errors = cleanup_all(
            base=str(base), runner=FakeRunner(), mounts_file=str(mounts_file)
        )
        assert errors == []
        assert any("Tap@interrupted" in ln for ln in lines)
        assert any("zfs destroy -r" in ln for ln in lines)
        # Sidecar cleared only after being reported.
        assert not (base / "cloud_backup-5.snapshot").exists()

    def test_unmounts_everything_under_the_base_deepest_first(self, tmp_path):
        base = tmp_path / "stage"
        base.mkdir()
        mounts_file = tmp_path / "mounts"
        mounts_file.write_text(
            f"tmpfs {base} tmpfs rw 0 0\n"
            f"tmpfs {base}/cloud_backup-5 tmpfs rw 0 0\n"
            f"tmpfs {base}/cloud_backup-5/apps tmpfs rw 0 0\n"
        )
        runner = FakeRunner()
        _lines, errors = cleanup_all(
            base=str(base), runner=runner, mounts_file=str(mounts_file)
        )
        assert errors == []
        order = [c[-1] for c in runner.calls if c[0] == "umount"]
        assert order == [
            f"{base}/cloud_backup-5/apps",
            f"{base}/cloud_backup-5",
            str(base),
        ]

    def test_keeps_sidecars_when_an_unmount_failed(self, tmp_path):
        # If a mount is stuck, the snapshot is still pinned — so the record of it
        # must survive for the next run (or the operator) to act on.
        base = tmp_path / "stage"
        base.mkdir()
        (base / "cloud_backup-5.snapshot").write_text("Tap@stuck")
        mounts_file = tmp_path / "mounts"
        mounts_file.write_text(f"tmpfs {base}/cloud_backup-5 tmpfs rw 0 0\n")

        class Stuck(FakeRunner):
            def __call__(self, cmd):
                self.calls.append(cmd)

                class R:
                    returncode = 32
                    stderr = "target is busy"

                return R

        _lines, errors = cleanup_all(
            base=str(base), runner=Stuck(), mounts_file=str(mounts_file)
        )
        assert errors, "a stuck unmount must be reported"
        assert (base / "cloud_backup-5.snapshot").exists()

    def test_is_a_noop_on_a_clean_system(self, tmp_path):
        mounts_file = tmp_path / "mounts"
        mounts_file.write_text("")
        lines, errors = cleanup_all(
            base=str(tmp_path / "absent"), runner=FakeRunner(),
            mounts_file=str(mounts_file),
        )
        assert errors == []
        assert lines == ["  None active."]


class TestCurrentMountsUnder:
    def test_matches_only_the_staging_subtree(self, tmp_path):
        mounts_file = tmp_path / "mounts"
        # "cloud_backup-50" must NOT match "cloud_backup-5".
        mounts_file.write_text(
            f"tmpfs {ROOT} tmpfs rw 0 0\n"
            "tmpfs /run/truecloud-nested/cloud_backup-50 tmpfs rw 0 0\n"
        )
        assert current_mounts_under(ROOT, mounts_file=str(mounts_file)) == [ROOT]


class TestStagingRootFor:
    def test_stable_per_task(self):
        assert staging_root_for("cloud_backup-5") == "/run/truecloud-nested/cloud_backup-5"

    def test_sanitises_path_separators(self):
        assert "/" not in staging_root_for("evil/name").rsplit("/", 1)[-1]

    @pytest.mark.parametrize("name", ["..", ".", "...", "/", ""])
    def test_dot_components_cannot_escape_the_staging_base(self, name):
        # os.path.join(BASE, "..") normalises to /run — teardown would rmdir it.
        root = staging_root_for(name)
        assert os.path.normpath(root).startswith("/run/truecloud-nested/")


class TestZfsAutomountKeepsSnapshotsBusy:
    """"dataset is busy" is EXPECTED, TRANSIENT, and used to orphan snapshots forever.

    Reading `<dataset>/.zfs/snapshot/<snap>/` makes ZFS **automount** that snapshot,
    and it stays mounted for zfs_expire_snapshot seconds (300 by default) after the
    last access. teardown() unmounts OUR bind mounts, but not the automount underneath
    -- so `zfs destroy` refuses with EBUSY for everything restic read recently.

    Observed on a real 256-snapshot tree: 253 swept cleanly, and the 3 datasets restic
    had touched last failed with "dataset is busy". cleanup_task then removed the
    sidecar anyway, so nothing would ever reclaim them. A few snapshots leaked per run,
    forever.
    """

    MOUNTS = (
        "tmpfs /run tmpfs rw 0 0\n"
        "Tap/apps/prometheus /mnt/Tap/apps/prometheus/.zfs/snapshot/snap1 zfs ro 0 0\n"
        "Tap/apps/standing/data /mnt/Tap/apps/standing/data/.zfs/snapshot/snap1 zfs ro 0 0\n"
        "Tap /mnt/Tap/.zfs/snapshot/snap1 zfs ro 0 0\n"
        "Tap/other /mnt/Tap/other/.zfs/snapshot/OTHER zfs ro 0 0\n"
    )

    def _mounts_file(self, tmp_path):
        p = tmp_path / "mounts"
        p.write_text(self.MOUNTS)
        return str(p)

    def test_it_finds_the_automounts_for_this_snapshot_only(self, tmp_path):
        import truecloud_nested as tn
        found = tn.snapdir_automounts("snap1", mounts_file=self._mounts_file(tmp_path))
        assert "/mnt/Tap/other/.zfs/snapshot/OTHER" not in found
        assert len(found) == 3

    def test_deepest_first(self, tmp_path):
        # A child's automount must be released before its parent's.
        import truecloud_nested as tn
        found = tn.snapdir_automounts("snap1", mounts_file=self._mounts_file(tmp_path))
        assert found[-1] == "/mnt/Tap/.zfs/snapshot/snap1"

    def test_release_snapdirs_unmounts_them(self, tmp_path):
        import truecloud_nested as tn
        called = []

        class R:
            returncode = 0
            stderr = ""

        def runner(cmd):
            called.append(cmd)
            return R()

        errs = tn.release_snapdirs("snap1", runner=runner,
                                   mounts_file=self._mounts_file(tmp_path))
        assert errs == []
        assert all(c[0] == "umount" for c in called)
        assert len(called) == 3


class BusyMiddleware(FakeMiddleware):
    """Deletes fail with EBUSY until `busy_until_attempt` passes -- like a ZFS
    automount expiring."""

    def __init__(self, snapshots, busy, busy_for=2):
        super().__init__(snapshots)
        self.busy = set(busy)
        self.busy_for = busy_for
        self.attempts = 0

    def call_sync(self, method, *args):
        if method == "zfs.snapshot.delete":
            name = args[0]
            opts = args[1] if len(args) > 1 else {}
            if opts.get("recursive"):
                raise RuntimeError("cannot destroy snapshot: dataset is busy")
            if name in self.busy:
                self.attempts += 1
                if self.attempts <= self.busy_for * len(self.busy):
                    raise RuntimeError(f"cannot destroy '{name}': dataset is busy")
        return super().call_sync(method, *args)


class TestDeleteRetriesAndReportsSurvivors:
    def test_a_transient_busy_is_retried_and_wins(self, monkeypatch):
        import truecloud_nested as tn
        monkeypatch.setattr(tn, "release_snapdirs", lambda *a, **k: [])

        mw = BusyMiddleware(
            ["Tap@snap", "Tap/apps@snap", "Tap/apps/prometheus@snap"],
            busy=["Tap/apps/prometheus@snap"], busy_for=1,
        )
        survivors = tn.delete_snapshot_tree(mw, "Tap@snap", sleep=lambda _s: None)
        assert survivors == []
        assert mw.snapshots == []

    def test_a_permanently_busy_snapshot_is_REPORTED_not_swallowed(self, monkeypatch):
        import truecloud_nested as tn
        monkeypatch.setattr(tn, "release_snapdirs", lambda *a, **k: [])

        mw = BusyMiddleware(
            ["Tap@snap", "Tap/apps/prometheus@snap"],
            busy=["Tap/apps/prometheus@snap"], busy_for=99,
        )
        survivors = tn.delete_snapshot_tree(mw, "Tap@snap", sleep=lambda _s: None)
        assert survivors == ["Tap/apps/prometheus@snap"]
        assert mw.snapshots == ["Tap/apps/prometheus@snap"]

    def test_the_automounts_are_released_before_deleting(self, monkeypatch):
        import truecloud_nested as tn
        order = []
        monkeypatch.setattr(tn, "release_snapdirs",
                            lambda name, **k: order.append(("release", name)) or [])
        mw = FakeMiddleware(["Tap@snap"])
        real = mw.call_sync

        def spy(method, *args):
            order.append((method, args[0] if args else None))
            return real(method, *args)

        mw.call_sync = spy
        tn.delete_snapshot_tree(mw, "Tap@snap", sleep=lambda _s: None)
        assert order[0] == ("release", "snap"), order


class TestSidecarSurvivesAnIncompleteSweep:
    def test_the_sidecar_is_KEPT_when_snapshots_could_not_be_deleted(
        self, tmp_path, monkeypatch
    ):
        # It is the ONLY record those snapshots exist. Removing it orphans them
        # permanently -- which is exactly what happened on the real box.
        import truecloud_nested as tn

        monkeypatch.setattr(tn, "STAGING_BASE", str(tmp_path))
        monkeypatch.setattr(tn, "release_snapdirs", lambda *a, **k: [])
        root = tn.staging_root_for("cloud_backup-5")
        os.makedirs(root, exist_ok=True)
        with open(sidecar_for(root), "w", encoding="utf-8") as fh:
            fh.write("Tap@snap")

        mw = BusyMiddleware(["Tap@snap", "Tap/apps/prometheus@snap"],
                            busy=["Tap/apps/prometheus@snap"], busy_for=99)
        monkeypatch.setattr(tn, "delete_snapshot_tree",
                            lambda m, s, logger=None: ["Tap/apps/prometheus@snap"])

        tn.cleanup_task(mw, "cloud_backup-5")
        assert os.path.exists(sidecar_for(root)), (
            "sidecar removed despite survivors — they are now orphaned forever"
        )

    def test_the_sidecar_is_removed_on_a_clean_sweep(self, tmp_path, monkeypatch):
        import truecloud_nested as tn

        monkeypatch.setattr(tn, "STAGING_BASE", str(tmp_path))
        root = tn.staging_root_for("cloud_backup-5")
        os.makedirs(root, exist_ok=True)
        with open(sidecar_for(root), "w", encoding="utf-8") as fh:
            fh.write("Tap@snap")

        monkeypatch.setattr(tn, "delete_snapshot_tree", lambda m, s, logger=None: [])
        tn.cleanup_task(FakeMiddleware(), "cloud_backup-5")
        assert not os.path.exists(sidecar_for(root))
