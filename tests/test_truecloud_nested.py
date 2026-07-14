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

import truecloud_nested as tn  # noqa: E402
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


@pytest.fixture(autouse=True)
def never_touch_the_real_system(monkeypatch):
    """A unit test must never shell out to the real box. This makes it impossible.

    Five tests silently did. `gc_stale_snapshots`'s DEFAULT lister runs
    `zfs list -t snapshot -r Tap` -- and on the NAS, `Tap` is the real pool, with
    2148 snapshots and a genuine leaked `cloud_backup-5` snapshot in it. Those tests
    passed on the dev box only because it has no `zfs` binary (FileNotFoundError,
    swallowed by a broad except), and would have gone RED on the one machine the
    release process requires them green on -- for reasons having nothing to do with
    the code. The same code path calls `umount`.

    Tests that need a system command inject one (`runner=` / `list_snapshots=`).

    It RECORDS and asserts at teardown rather than raising, because raising would be
    swallowed: `gc_stale_snapshots` catches broad `Exception` around its enumeration
    (deliberately — it must fail toward collecting nothing). That swallow is exactly
    what let five tests shell out unnoticed, so the check must survive it.
    """
    attempted = []

    def forbidden(cmd):
        attempted.append(cmd)
        raise AssertionError(f"real system command in a unit test: {cmd!r}")

    monkeypatch.setattr(tn, "_run", forbidden)

    # ...and the REAL mount table, which is the other half. Nineteen tests were
    # reading /proc/self/mounts: harmless here, but on the NAS a name that happened to
    # match would send `release_snapdirs`/`teardown` off to run a real `umount`.
    #
    # Patching the module attribute only works because these are late-bound now. A
    # default argument (`mounts_file="/proc/self/mounts"`) is frozen into __defaults__
    # at def time and monkeypatching cannot reach it -- which is exactly why the first
    # version of this fixture looked like it worked and did not.
    monkeypatch.setattr(tn, "MOUNTS_FILE", os.devnull)

    # ...and never really sleep. The retry loop waits 5s between attempts for ZFS's
    # automount window to expire; a test that hits it burns 20 real seconds and tells
    # you nothing. (Same frozen-default trap: `sleep=time.sleep` in the signature could
    # not be intercepted at all until it was late-bound.)
    slept = []
    monkeypatch.setattr(tn.time, "sleep", lambda s: slept.append(s))

    yield

    assert not attempted, (
        "this test ran real system commands: "
        + "; ".join(repr(c) for c in attempted)
        + ". Inject a fake (runner= / list_snapshots=) -- on the NAS these hit the "
        "REAL pool, and the suite must not depend on the machine it runs on."
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


class _FrameworkCRUDService:
    """Stands in for middlewared's real `CRUDService` base class.

    This shape is load-bearing, and a fake without it is worse than no fake at all.

    The real CRUDService defines `delete` on the BASE class and dispatches to
    `self.do_delete` at call time, so a bound `delete` exists on EVERY subclass —
    including one whose `do_delete` iX has deleted. A fake that is just a bare object
    with a `delete` attribute cannot express that, so a runtime check that asks
    `hasattr(service, "delete")` would look CORRECT against the fake while being
    useless against the real thing. That is exactly what happened: the first version
    of this fix passed its test and did nothing on a real box.

    `__module__` is set to middlewared's real framework package because that is how
    `_defines_delete` tells plumbing apart from an implementation.
    """

    def delete(self, *args, **kwargs):      # the generic dispatcher -> self.do_delete
        raise NotImplementedError


_FrameworkCRUDService.__module__ = "middlewared.service.crud_service"


class _FakeSnapshotService(_FrameworkCRUDService):
    """What `middleware.get_service("<ns>")` hands back: a plugin CRUDService.

    `delete_method=None` models the dangerous case — iX guts the concrete method but
    leaves the service registered (they have already done this to
    `pool.snapshot.do_update` on master). The inherited `delete` is still there and
    still callable; only the implementation is gone.
    """

    def __init__(self, delete_method):
        if delete_method:
            # Define it on the CLASS, not the instance: `_defines_delete` walks the
            # MRO's __dict__s, exactly as it must against a real service.
            cls = type(
                "FakePluginSnapshotService", (_FrameworkCRUDService,),
                {delete_method: lambda self, *a, **kw: None},
            )
            cls.__module__ = "middlewared.plugins.pool_.snapshot"
            self.__class__ = cls


class FakeMiddleware:
    """middlewared as this module actually uses it: `call_sync`, from a thread.

    The module is synchronous on purpose -- see the orchestration note in
    truecloud_nested.py. TrueNAS <= 25.10 reaches it through
    `await middleware.run_in_thread(...)` and TrueNAS 26 calls it directly, but the
    logic below the boundary is the same code either way, so it is tested once.

    `snapshot_ns` picks which middleware GENERATION this is, because they do not
    agree on what the snapshot service is called:

        pool.snapshot   25.10 and 26      (26 has ONLY this -- plugins/zfs_/ is gone)
        zfs.snapshot    24.10 and 25.04   (pool.snapshot does not exist yet)

    A method in the namespace this box does NOT have raises, exactly as middleware
    does ("Method does not exist"). That is what makes the runtime picker testable:
    a module that guessed wrong would blow up here instead of silently orphaning
    snapshots on somebody's NAS.
    """

    def __init__(self, snapshots=None, snapshot_ns="pool.snapshot",
                 delete_method="delete"):
        self.snapshots = list(snapshots or [])
        self.calls = []
        self.logger = None
        self.snapshot_ns = snapshot_ns
        #: A CRUDService exposes `delete` from a method NAMED `do_delete`. Both
        #: spellings are live across the matrix, and the runtime must accept either
        #: -- it resolves the namespace by asking whether it can DELETE, not merely
        #: whether the service is registered.
        self.delete_method = delete_method

    def get_service(self, name):
        if name != self.snapshot_ns:
            raise KeyError(name)      # middleware raises KeyError for an unknown ns
        return _FakeSnapshotService(self.delete_method)

    def list_snapshots(self, dataset):
        """Stands in for `zfs list -t snapshot -r <dataset>`.

        Enumeration comes from ZFS now, NOT from middleware -- middleware's query
        hides internal datasets, and a sweep that cannot see a snapshot can never
        collect it. Passed in as `list_snapshots=` so the seam is explicit.
        """
        return [
            n for n in self.snapshots
            if n.split("@")[0] == dataset or n.startswith(dataset + "/")
        ]

    def call_sync(self, method, *args):
        self.calls.append((method, args))
        namespace, _, op = method.rpartition(".")

        if namespace != self.snapshot_ns:
            raise RuntimeError("Method does not exist")

        if op == "query":
            return [{"name": n} for n in self.snapshots]
        if op == "delete":
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
        delete_snapshot_tree(mw, "Tap@snap", list_snapshots=mw.list_snapshots)
        assert mw.snapshots == ["Tap@keepme"]

    def test_is_idempotent_when_stock_already_removed_the_parent(self):
        # Stock's finally can win the race once our mounts are released.
        mw = FakeMiddleware(["Tap/apps@snap", "Tap/apps/lidarr@snap"])
        delete_snapshot_tree(mw, "Tap@snap", list_snapshots=mw.list_snapshots)
        assert mw.snapshots == []

    def test_uses_a_single_recursive_delete_not_252_individual_ones(self):
        # 252 sequential deletes are slow AND not atomic: a run killed part-way
        # through leaves exactly the orphans this function exists to prevent.
        mw = FakeMiddleware(["Tap@snap", "Tap/apps@snap", "Tap/apps/lidarr@snap"])
        listed = []

        def counting_lister(dataset):
            listed.append(dataset)
            return mw.list_snapshots(dataset)

        delete_snapshot_tree(mw, "Tap@snap", list_snapshots=counting_lister)
        assert mw.snapshots == []
        deletes = [a for m, a in mw.calls if m.endswith(".delete")]
        assert len(deletes) == 1, "should be ONE recursive call, not one per snapshot"
        assert deletes[0][1] == {"recursive": True}
        assert listed == ["Tap"], (
            "the fast path must enumerate EXACTLY ONCE -- to CONFIRM the tree is gone. "
            "Zero would mean trusting a delete that returned without raising, and a "
            "silent no-op delete then makes cleanup_task drop the sidecar and orphan "
            "~250 snapshots forever. More than once is waste."
        )

    def test_survives_recursive_and_enumeration_failure_by_deleting_the_parent(self):
        # Both the recursive delete AND the ZFS enumeration fail. The sweep must still
        # remove the parent rather than give up entirely.
        class NoRecursive(FakeMiddleware):
            def call_sync(self, method, *args):
                if method.endswith(".delete") and len(args) > 1:
                    raise RuntimeError("recursive delete unavailable")
                return super().call_sync(method, *args)

        def broken_lister(_dataset):
            raise tn.ZfsError("boom")

        mw = NoRecursive(["Tap@snap"])
        delete_snapshot_tree(mw, "Tap@snap", list_snapshots=broken_lister)
        assert mw.snapshots == [], "must fall back to at least deleting the parent"

    def test_leaves_unrelated_snapshots_alone_when_the_tree_is_gone(self):
        mw = FakeMiddleware(["Tap@unrelated"])
        delete_snapshot_tree(mw, "Tap@snap", list_snapshots=mw.list_snapshots)
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

        _mw = FakeMiddleware()
        tn.stage_nested(
            _mw, "/mnt/Tap", "Tap@snap", "Tap", "/mnt/Tap",
            "cloud_backup-5", DATASETS, list_snapshots=_mw.list_snapshots,
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
            "cloud_backup-5", DATASETS, list_snapshots=mw.list_snapshots,
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
            _mw = FakeMiddleware()
            tn.stage_nested(
                _mw, "/mnt/Tap", "Tap@snap", "Tap", "/mnt/Tap",
                "cloud_backup-5", DATASETS, list_snapshots=_mw.list_snapshots,
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

        cleanup_task(mw, "cloud_backup-5", list_snapshots=mw.list_snapshots)

        assert mw.snapshots == []
        assert not os.path.exists(sidecar_for(root))

    def test_is_a_noop_when_never_staged(self, tmp_path, monkeypatch):
        import truecloud_nested as tn

        monkeypatch.setattr(tn, "STAGING_BASE", str(tmp_path / "nope"))
        mw = FakeMiddleware(["Tap@snap"])
        cleanup_task(mw, "cloud_backup-5", list_snapshots=mw.list_snapshots)
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
        if method.endswith(".delete"):
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
        survivors = tn.delete_snapshot_tree(mw, "Tap@snap", sleep=lambda _s: None, list_snapshots=mw.list_snapshots)
        assert survivors == []
        assert mw.snapshots == []

    def test_a_permanently_busy_snapshot_is_REPORTED_not_swallowed(self, monkeypatch):
        import truecloud_nested as tn
        monkeypatch.setattr(tn, "release_snapdirs", lambda *a, **k: [])

        mw = BusyMiddleware(
            ["Tap@snap", "Tap/apps/prometheus@snap"],
            busy=["Tap/apps/prometheus@snap"], busy_for=99,
        )
        survivors = tn.delete_snapshot_tree(mw, "Tap@snap", sleep=lambda _s: None, list_snapshots=mw.list_snapshots)
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
        tn.delete_snapshot_tree(mw, "Tap@snap", sleep=lambda _s: None, list_snapshots=mw.list_snapshots)
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
                            lambda m, s, logger=None, **kw: ["Tap/apps/prometheus@snap"])

        tn.cleanup_task(mw, "cloud_backup-5", list_snapshots=mw.list_snapshots)
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

        monkeypatch.setattr(tn, "delete_snapshot_tree", lambda m, s, logger=None, **kw: [])
        _mw = FakeMiddleware()
        tn.cleanup_task(_mw, "cloud_backup-5", list_snapshots=_mw.list_snapshots)
        assert not os.path.exists(sidecar_for(root))


class TestTheSidecarCarriesEveryPendingTree:
    """The sidecar holds a LIST, and that is a bug fix, not a generalisation.

    It used to hold ONE snapshot. So a run that reclaimed an older tree, FAILED to
    finish reclaiming it, and then recorded its own snapshot would **overwrite the only
    record of the survivor** — orphaning it permanently, via the exact code written to
    prevent orphans.

    Observed live: a snapshot survived one run; the next run's reclaim also failed
    (ZFS's 300s automount window had not elapsed, because the two runs were minutes
    apart); the record was overwritten; the snapshot was orphaned for good.
    """

    def test_round_trips_a_list(self, tmp_path):
        import truecloud_nested as tn
        root = str(tmp_path / "cloud_backup-5")
        tn._write_sidecar(root, ["Tap@a", "Tap@b"])
        assert tn._read_sidecar(root) == ["Tap@a", "Tap@b"]

    def test_reads_the_old_single_line_format(self, tmp_path):
        # Boxes upgrading from an older version have a one-line sidecar on disk.
        import truecloud_nested as tn
        root = str(tmp_path / "cloud_backup-5")
        os.makedirs(os.path.dirname(sidecar_for(root)), exist_ok=True)
        with open(sidecar_for(root), "w", encoding="utf-8") as fh:
            fh.write("Tap@legacy")
        assert tn._read_sidecar(root) == ["Tap@legacy"]

    def test_a_failed_reclaim_is_carried_forward_not_overwritten(
        self, tmp_path, monkeypatch
    ):
        # THE bug. stage_nested reclaims an old tree, cannot finish, then records its
        # own snapshot -- the survivor must still be in the sidecar afterwards.
        import truecloud_nested as tn

        monkeypatch.setattr(tn, "STAGING_BASE", str(tmp_path))
        root = tn.staging_root_for("cloud_backup-5")
        os.makedirs(os.path.dirname(root), exist_ok=True)
        tn._write_sidecar(root, ["Tap@old"])

        # The reclaim of Tap@old leaves one snapshot behind (still busy).
        monkeypatch.setattr(
            tn, "delete_snapshot_tree",
            lambda m, s, logger=None, **kw: ["Tap/apps/x@old"] if s == "Tap@old" else [],
        )
        stub_core(monkeypatch, tn, plan=([("/src", root)], []))

        _mw = FakeMiddleware()
        tn.stage_nested(_mw, "/mnt/Tap", "Tap@new", "Tap", "/mnt/Tap",
                        "cloud_backup-5", DATASETS,
                        list_snapshots=_mw.list_snapshots)

        recorded = tn._read_sidecar(root)
        assert "Tap/apps/x@old" in recorded, (
            "the failed reclaim's survivor was dropped — orphaned forever"
        )
        assert "Tap@new" in recorded, "our own snapshot must also be recorded"

    def test_cleanup_sweeps_every_pending_tree_and_records_only_survivors(
        self, tmp_path, monkeypatch
    ):
        import truecloud_nested as tn

        monkeypatch.setattr(tn, "STAGING_BASE", str(tmp_path))
        root = tn.staging_root_for("cloud_backup-5")
        os.makedirs(root, exist_ok=True)
        tn._write_sidecar(root, ["Tap@old", "Tap@new"])

        swept = []

        def fake_delete(m, s, logger=None, **kw):
            swept.append(s)
            return ["Tap/apps/x@new"] if s == "Tap@new" else []

        monkeypatch.setattr(tn, "delete_snapshot_tree", fake_delete)
        _mw = FakeMiddleware()
        tn.cleanup_task(_mw, "cloud_backup-5", list_snapshots=_mw.list_snapshots)

        assert swept == ["Tap@old", "Tap@new"], "both pending trees must be swept"
        # Only the SURVIVOR is written back -- re-recording Tap@old would make every
        # future run re-sweep a tree that is already gone.
        assert tn._read_sidecar(root) == ["Tap/apps/x@new"]

    def test_a_fully_clean_sweep_removes_the_sidecar(self, tmp_path, monkeypatch):
        import truecloud_nested as tn

        monkeypatch.setattr(tn, "STAGING_BASE", str(tmp_path))
        root = tn.staging_root_for("cloud_backup-5")
        os.makedirs(root, exist_ok=True)
        tn._write_sidecar(root, ["Tap@a", "Tap@b"])
        monkeypatch.setattr(tn, "delete_snapshot_tree", lambda m, s, logger=None, **kw: [])

        _mw = FakeMiddleware()
        tn.cleanup_task(_mw, "cloud_backup-5", list_snapshots=_mw.list_snapshots)
        assert not os.path.exists(sidecar_for(root))

    def test_cleanup_all_reports_each_pending_snapshot_on_its_own_line(self, tmp_path):
        # It formats them for a human during uninstall. A list rendered into an
        # f-string would print "['Tap@a', 'Tap@b']" at them.
        import truecloud_nested as tn
        root = str(tmp_path / "cloud_backup-5")
        tn._write_sidecar(root, ["Tap@a", "Tap@b"])

        lines, _errors = tn.cleanup_all(
            base=str(tmp_path),
            glob_fn=lambda _p: [sidecar_for(root)],
            mounts_file=os.devnull,
        )
        notes = [ln for ln in lines if "left snapshot" in ln]
        assert len(notes) == 2
        assert "'Tap@a'" in notes[0] and "'Tap@b'" in notes[1]
        assert "[" not in "".join(notes)


class TestGarbageCollectorSelection:
    """`stale_snapshot_names` DELETES DATA on a name match.

    A name match is a weaker claim than a recorded fact, so every way it could be wrong
    is a test. It exists because the sidecar — which IS a recorded fact — lives in /run,
    which is tmpfs: a reboot mid-backup destroys it and orphans a 250-snapshot tree with
    nothing left pointing at it. This is the only thing that would ever find those.
    """

    import datetime as _dt
    NOW = _dt.datetime(2026, 7, 14, 12, 0, 0, tzinfo=_dt.UTC)
    CURRENT = "Tap@cloud_backup-5-20260714115900"        # 1 minute ago
    OLD = "Tap/apps/x@cloud_backup-5-20260713030000"     # ~33 hours ago

    def collect(self, names, **kw):
        import truecloud_nested as tn
        return tn.stale_snapshot_names(
            "cloud_backup-5", self.CURRENT, names, self.NOW, **kw
        )

    def test_it_collects_our_own_leftovers(self):
        assert self.collect([self.OLD]) == [self.OLD]

    def test_it_NEVER_touches_the_current_run(self):
        # Both the parent and its children share the current snapname.
        names = [self.CURRENT, "Tap/apps/x@cloud_backup-5-20260714115900"]
        assert self.collect(names) == []

    def test_it_NEVER_touches_a_periodic_snapshot(self):
        assert self.collect(["Tap/apps/x@auto-2026-07-13_03-00"]) == []

    def test_it_NEVER_touches_a_human_made_snapshot(self):
        assert self.collect(["Tap@before-i-broke-everything"]) == []

    def test_it_NEVER_touches_another_TASK(self):
        # cloud_backup-5 must not match cloud_backup-50. This is why the prefix
        # carries the trailing dash.
        assert self.collect(["Tap/apps/x@cloud_backup-50-20260713030000"]) == []
        assert self.collect(["Tap/apps/x@cloud_backup-7-20260713030000"]) == []

    def test_it_NEVER_touches_a_one_time_backup(self):
        assert self.collect(["Tap@cloud_backup-onetime-20260713030000"]) == []

    def test_it_NEVER_touches_a_snapshot_that_is_MOUNTED(self):
        # An in-flight run pins its own snapshots. This — not the age heuristic — is
        # what actually protects a concurrent backup.
        assert self.collect([self.OLD], in_use={self.OLD}) == []

    def test_it_NEVER_touches_a_snapshot_younger_than_the_minimum_age(self):
        # Covers the seconds-long window between `zfs snapshot -r` and the mounts
        # appearing, when a live run's snapshots look exactly like garbage.
        young = "Tap/apps/x@cloud_backup-5-20260714113000"   # 30 minutes ago
        assert self.collect([young]) == []
        assert self.collect([young], min_age=60) == [young]

    def test_a_name_it_cannot_parse_is_left_alone(self):
        assert self.collect(["Tap@cloud_backup-5-not-a-timestamp"]) == []
        assert self.collect(["Tap@cloud_backup-5-"]) == []

    def test_a_realistic_mixed_pool(self):
        names = [
            self.CURRENT,                                       # ours, running
            "Tap/apps/x@cloud_backup-5-20260714115900",         # ours, running (child)
            self.OLD,                                           # ours, orphaned  <-
            "Tap/apps/y@cloud_backup-5-20260712030000",         # ours, orphaned  <-
            "Tap/apps/x@auto-2026-07-13_03-00",                 # periodic
            "Tap/apps/x@cloud_backup-7-20260713030000",         # another task
            "Tap@manual-keepme",                                # human
        ]
        assert sorted(self.collect(names)) == sorted(
            [self.OLD, "Tap/apps/y@cloud_backup-5-20260712030000"]
        )


class TestMountedSnapshots:
    def test_it_reads_snapshot_names_out_of_the_mount_table(self, tmp_path):
        import truecloud_nested as tn
        mounts = tmp_path / "mounts"
        mounts.write_text(
            "tmpfs /run tmpfs rw 0 0\n"
            "Tap/apps/x@snap1 /run/truecloud-nested/t/apps/x zfs ro 0 0\n"
            "Tap/apps/y@snap1 /mnt/Tap/apps/y/.zfs/snapshot/snap1 zfs ro 0 0\n"
            "Tap/live /mnt/Tap/live zfs rw 0 0\n"
        )
        live = tn.mounted_snapshots(str(mounts))
        assert live == {"Tap/apps/x@snap1", "Tap/apps/y@snap1"}
        assert "Tap/live" not in live      # a live dataset is not a snapshot


class TestGarbageCollectorExecution:
    def test_it_deletes_the_stale_ones_and_nothing_else(self, monkeypatch, tmp_path):
        import datetime as dt
        import truecloud_nested as tn

        mounts = tmp_path / "mounts"
        mounts.write_text("")
        now = dt.datetime(2026, 7, 14, 12, 0, 0, tzinfo=dt.UTC)

        mw = FakeMiddleware([
            "Tap@cloud_backup-5-20260714115900",             # current run
            "Tap/apps/x@cloud_backup-5-20260713030000",      # orphan   <-
            "Tap/apps/x@auto-2026-07-13_03-00",              # periodic
            "Tap/apps/x@cloud_backup-7-20260713030000",      # other task
        ])
        remaining = tn.gc_stale_snapshots(
            mw, "cloud_backup-5", "Tap@cloud_backup-5-20260714115900",
            now=now, mounts_file=str(mounts),
            list_snapshots=mw.list_snapshots,
        )
        assert remaining == []
        assert mw.snapshots == [
            "Tap@cloud_backup-5-20260714115900",
            "Tap/apps/x@auto-2026-07-13_03-00",
            "Tap/apps/x@cloud_backup-7-20260713030000",
        ]

    def test_a_busy_orphan_is_reported_not_swallowed(self, monkeypatch, tmp_path):
        import datetime as dt
        import truecloud_nested as tn

        mounts = tmp_path / "mounts"
        mounts.write_text("")
        now = dt.datetime(2026, 7, 14, 12, 0, 0, tzinfo=dt.UTC)
        orphan = "Tap/apps/x@cloud_backup-5-20260713030000"

        mw = BusyMiddleware(
            ["Tap@cloud_backup-5-20260714115900", orphan],
            busy=[orphan], busy_for=99,
        )
        remaining = tn.gc_stale_snapshots(
            mw, "cloud_backup-5", "Tap@cloud_backup-5-20260714115900",
            now=now, mounts_file=str(mounts),
            list_snapshots=mw.list_snapshots,
        )
        assert remaining == [orphan]

    def test_it_collects_NOTHING_when_the_query_fails(self, tmp_path):
        # Cannot enumerate => cannot know what is ours => delete nothing.
        import datetime as dt
        import truecloud_nested as tn

        mounts = tmp_path / "mounts"
        mounts.write_text("")

        # The ENUMERATION fails -- which is now a `zfs list` that cannot run, not a
        # middleware query. (This test used to fake a failure of `.query`, a call
        # production no longer makes, so it passed no matter what the code did.)
        def broken_lister(_dataset):
            raise tn.ZfsError("cannot open 'Tap': pool I/O is currently suspended")

        mw = FakeMiddleware(["Tap/apps/x@cloud_backup-5-20260713030000"])
        assert tn.gc_stale_snapshots(
            mw,
            "cloud_backup-5", "Tap@cloud_backup-5-20260714115900",
            now=dt.datetime(2026, 7, 14, 12, 0, 0, tzinfo=dt.UTC),
            mounts_file=str(mounts),
            list_snapshots=broken_lister,
        ) == []
        assert mw.snapshots, (
            "cannot enumerate => cannot know what is ours => must delete NOTHING"
        )


class TestEnumerationComesFromZfsNotMiddleware:
    """The bug that a source check can never catch, found only by running it.

    Porting the deleted private `zfs.dataset.query` to the public
    `pool.dataset.query` looked obviously right: the method exists, it is
    documented, iX will not delete it. Every test passed and `compat.py` went green
    on TrueNAS 26.

    It was wrong. The public query applies a VISIBILITY POLICY -- on a real box it
    returns 205 of 274 datasets, hiding `ix-apps/*`, `.system/*` and `.ix-virt/*`.
    On the production pool that is 84 of 270, and `ix-apps` holds LIVE APPLICATION
    DATA. The staging plan would have omitted every one of them, and `plan_staging`
    would never have seen them, so they would not even appear in `skipped`. A green
    backup, silently missing data -- the precise failure this module exists to
    prevent.

    The snapshot query lies the same way, so the sweep would orphan one snapshot per
    hidden dataset, forever.

    Hence: READ from ZFS, MUTATE through middleware. These tests hold that line.
    """

    def test_query_filesystems_shells_out_to_zfs(self):
        import truecloud_nested as tn

        seen = []

        class R:
            returncode = 0
            stdout = (
                "scratch\t/mnt/scratch\tyes\n"
                "scratch/ix-apps\t/mnt/scratch/ix-apps\tyes\n"   # middleware HIDES this one
                "scratch/.system\tlegacy\tno\n"
            )
            stderr = ""

        def runner(cmd):
            seen.append(cmd)
            return R()

        rows = tn.query_filesystems(runner=runner)

        assert seen and seen[0][0] == "zfs", "must read ZFS, not call middleware"
        names = [r["name"] for r in rows]
        assert "scratch/ix-apps" in names, (
            "ix-apps is exactly what pool.dataset.query hides, and exactly what "
            "holds live app data. If it is not here the backup omits it silently."
        )
        # ...and it still speaks the shape the planner expects.
        row = next(r for r in rows if r["name"] == "scratch/.system")
        assert row["properties"]["mountpoint"]["value"] == "legacy"
        assert row["properties"]["mounted"]["value"] == "no"

    def test_a_failing_zfs_raises_rather_than_returning_an_empty_list(self):
        # The whole failure class in one assertion. "No datasets" and "the command
        # broke" must never look the same: a caller that cannot tell them apart
        # stages nothing, sweeps nothing, and reports success.
        import truecloud_nested as tn

        class R:
            returncode = 1
            stdout = ""
            stderr = "cannot open 'scratch': no such pool"

        with pytest.raises(tn.ZfsError, match="no such pool"):
            tn.query_filesystems(runner=lambda cmd: R())

        with pytest.raises(tn.ZfsError):
            tn.list_snapshot_names("scratch", runner=lambda cmd: R())

    def test_list_snapshot_names_reads_the_whole_tree_from_zfs(self):
        import truecloud_nested as tn

        seen = []

        class R:
            returncode = 0
            stdout = "scratch@s\nscratch/ix-apps@s\n"
            stderr = ""

        def runner(cmd):
            seen.append(cmd)
            return R()

        names = tn.list_snapshot_names("scratch", runner=runner)
        assert names == ["scratch@s", "scratch/ix-apps@s"]
        assert "-t" in seen[0] and "snapshot" in seen[0] and "-r" in seen[0]
        assert "scratch/ix-apps@s" in names, (
            "pool.snapshot.query hides this; a sweep that cannot see it orphans it "
            "on every single run, forever"
        )


class TestTheProductionWiringIsWhatWeThinkItIs:
    """Tests of a seam prove nothing if production stops using the seam.

    An audit mutation-tested this suite and found two regressions that reinstate the
    exact bug this module exists to prevent, while all 293 tests still passed:

      * swap `delete_snapshot_tree`/`gc_stale_snapshots`'s DEFAULT enumerator for one
        that returns [] (which is what middleware's filtered query does for the 84
        hidden datasets) -- green, because every test injected its own.
      * put `pool.dataset.query` back into apply.sh's injected block -- green, because
        nothing asserted what that block enumerates with.

    Both are pinned here. These tests are about the WIRING, not the logic.
    """

    def test_the_default_snapshot_enumerator_is_the_ZFS_one(self, monkeypatch, tmp_path):
        # Called with no `list_snapshots=`, exactly as production calls it.
        called = []
        monkeypatch.setattr(tn, "list_snapshot_names",
                            lambda ds, **kw: called.append(ds) or [])

        mw = FakeMiddleware(["Tap@snap"])

        class NoRecursive(FakeMiddleware):
            def call_sync(self, method, *args):
                if method.endswith(".delete") and len(args) > 1:
                    raise RuntimeError("recursive delete unavailable")
                return super().call_sync(method, *args)

        tn.delete_snapshot_tree(NoRecursive(["Tap@snap"]), "Tap@snap")
        assert called and set(called) == {"Tap"}, (
            "delete_snapshot_tree's fallback sweep must enumerate from ZFS by default. "
            "If it defaults to a middleware query, it cannot see the internal datasets "
            "and orphans one snapshot per hidden dataset on every run."
        )

        called.clear()
        mounts = tmp_path / "mounts"
        mounts.write_text("")
        monkeypatch.setattr(tn, "STAGING_BASE", str(tmp_path))
        tn.gc_stale_snapshots(mw, "cloud_backup-5", "Tap@cloud_backup-5-2026",
                              mounts_file=str(mounts))
        assert called and set(called) == {"Tap"}, (
            "the GC must enumerate from ZFS by default too"
        )

    def test_the_injected_block_enumerates_from_ZFS_not_middleware(self):
        # apply.sh is a shell file holding the Python that gets injected into
        # middlewared. Assert on what that block actually says.
        with open(os.path.join(os.path.dirname(__file__), "..", "patch", "apply.sh"),
                  encoding="utf-8") as fh:
            src = fh.read()

        assert "_tc_nested.query_filesystems(" in src, (
            "the staging plan must be built from query_filesystems() (which reads ZFS)"
        )

        # Match the METHOD NAME however it is quoted. An earlier version of this test
        # only looked for the double-quoted form, so a single-quoted
        # `call_sync('pool.snapshot.query')` -- including one passed in as the sweep's
        # lister, which is the catastrophic case -- sailed straight through it.
        import re
        code = "\n".join(
            ln for ln in src.splitlines() if not ln.lstrip().startswith("#")
        )
        offenders = re.findall(r"\b(?:pool|zfs)\.(?:dataset|snapshot)\.query\b", code)
        assert not offenders, (
            f"apply.sh references {sorted(set(offenders))}. Middleware's queries apply "
            f"a visibility policy and hide ix-apps/*, .system/*, .ix-virt/* -- 84 of "
            f"270 datasets on a real pool, including live app data. Enumerating from "
            f"them omits those datasets from the backup SILENTLY, and sweeping from "
            f"them orphans one snapshot per hidden dataset on every run."
        )


class TestTheSnapshotNamespaceIsResolvedNotAssumed:
    """24.10/25.04 have only `zfs.snapshot`; 26 has only `pool.snapshot`.

    Every one of these mutations used to pass the whole suite, because no test ever
    built a non-default middleware generation:

      * `_can_delete` -> always True  (breaks 24.10: picks a namespace that isn't there)
      * SNAPSHOT_SERVICES reversed    (breaks 26)
      * `snapshot_service` guessing instead of raising
    """

    def test_a_24_10_box_deletes_through_zfs_snapshot(self):
        mw = FakeMiddleware(["Tap@snap", "Tap/apps@snap"], snapshot_ns="zfs.snapshot")
        assert tn.delete_snapshot_tree(mw, "Tap@snap",
                                       list_snapshots=mw.list_snapshots) == []
        assert mw.snapshots == []
        methods = {m for m, _a in mw.calls}
        assert methods == {"zfs.snapshot.delete"}, (
            f"a 24.10 box has no pool.snapshot; called {methods}"
        )

    def test_a_26_box_deletes_through_pool_snapshot(self):
        mw = FakeMiddleware(["Tap@snap", "Tap/apps@snap"], snapshot_ns="pool.snapshot")
        assert tn.delete_snapshot_tree(mw, "Tap@snap",
                                       list_snapshots=mw.list_snapshots) == []
        assert {m for m, _a in mw.calls} == {"pool.snapshot.delete"}

    def test_a_CRUDService_that_only_defines_do_delete_is_usable(self):
        # `delete` is exposed FROM a method named `do_delete`. compat.py accepts both,
        # so the runtime must too, or they disagree about the same box.
        mw = FakeMiddleware(["Tap@snap"], delete_method="do_delete")
        assert tn.snapshot_service(mw) == "pool.snapshot"

    def test_a_registered_service_that_CANNOT_delete_is_not_chosen(self):
        # The subtle one. `get_service()` only proves the namespace is registered.
        # iX has already gutted a method while keeping its service
        # (`pool.snapshot.do_update` on master). If the runtime settled for "the
        # service exists", it would pick pool.snapshot, fail every delete, and orphan
        # the whole tree -- while compat.py, which checks the METHOD, fell through to
        # zfs.snapshot and reported the box healthy.
        class GuttedPoolSnapshot(FakeMiddleware):
            def get_service(self, name):
                if name == "pool.snapshot":
                    # Registered, and `delete` IS still there -- inherited from
                    # CRUDService, which dispatches to a `do_delete` that no longer
                    # exists. This is the shape middlewared actually produces, and a
                    # naive `hasattr(service, "delete")` says YES to it.
                    gutted = _FakeSnapshotService(None)
                    assert callable(gutted.delete), (
                        "the fake must keep the inherited dispatcher, or it cannot "
                        "reproduce the bug"
                    )
                    return gutted
                if name == "zfs.snapshot":
                    return super().get_service("zfs.snapshot")
                raise KeyError(name)

        mw = GuttedPoolSnapshot(["Tap@snap"], snapshot_ns="zfs.snapshot")
        assert tn.snapshot_service(mw) == "zfs.snapshot", (
            "must fall through to a namespace that can actually delete"
        )

    def test_no_usable_namespace_REFUSES_rather_than_guessing(self):
        class Neither(FakeMiddleware):
            def get_service(self, name):
                raise KeyError(name)

        with pytest.raises(StagingError, match="no usable snapshot delete"):
            tn.snapshot_service(Neither())

    def test_the_runtime_list_and_the_compat_manifest_cannot_drift(self):
        # tools/compat.py claims "the runtime picks the same way ... so what this
        # checks and what the patch does cannot drift apart." Nothing enforced that,
        # and reordering SNAPSHOT_SERVICES silently broke the claim. Now it is bound.
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
        import compat

        call = next(c for c in compat.MIDDLEWARE_CALLS if c.id == "call-snapshot-delete")
        checked = [compat.MiddlewareCall.namespace_of(m) for m, _p in call.options]
        assert checked == list(tn.SNAPSHOT_SERVICES), (
            f"compat checks {checked} but the runtime tries {list(tn.SNAPSHOT_SERVICES)} "
            f"-- in THIS order. They must agree, or CI blesses a box that fails at run "
            f"time."
        )
        assert set(compat.DELETE_NAMES) == set(tn.DELETE_METHODS), (
            "compat and the runtime must accept the same delete spellings"
        )


class TestWeOwnTheSweepEvenWhenWeDoNotStage:
    """TrueNAS 26 leak: stock's `recursive` rule is not the patch's `nested` rule.

    <= 25.10  stock's create_snapshot calls get_dataset_recursive() -- the same
              function this module vendors. "Stock went recursive" and "we have
              something to stage" were the SAME question, so a non-staged snapshot
              provably had no children and stock's non-recursive delete was correct.

    26        stock uses filesystem.statfs: recursive = (path == the dataset's
              mountpoint). Now the rules disagree. A dataset whose only descendants
              are ZVOLs or legacy/none-mountpoint datasets gets a RECURSIVE snapshot,
              while get_dataset_recursive() reports nothing to stage -- neither kind
              is a mounted filesystem under `path`.

    Stock then destroys the PARENT ONLY. With no staging tree there was no sidecar,
    and the GC only ever ran from stage_nested -- so nothing on the box would ever
    have found the children. One orphan per zvol/legacy descendant, on every run,
    forever, while the backup reports SUCCESS.

    Ownership of the sweep is therefore NOT conditional on staging.
    """

    def test_own_snapshot_records_a_snapshot_it_did_not_stage(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tn, "STAGING_BASE", str(tmp_path))
        mw = FakeMiddleware(["Tap@cloud_backup-5-2026", "Tap/vm-zvol@cloud_backup-5-2026"])
        root = tn.own_snapshot(mw, "cloud_backup-5", "Tap@cloud_backup-5-2026",
                               list_snapshots=mw.list_snapshots)

        assert tn._read_sidecar(root) == ["Tap@cloud_backup-5-2026"], (
            "the snapshot must be RECORDED even though nothing was staged -- the "
            "sidecar is the only thing that makes the sweep happen"
        )

    def test_the_recorded_snapshot_is_then_actually_swept(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tn, "STAGING_BASE", str(tmp_path))

        # A recursive snapshot of a dataset whose only child is a ZVOL: exactly the
        # 26 case. Nothing to stage, but the children are real.
        mw = FakeMiddleware([
            "Tap@cloud_backup-5-2026",
            "Tap/vm-zvol@cloud_backup-5-2026",
            "Tap/legacy-ds@cloud_backup-5-2026",
        ])
        tn.own_snapshot(mw, "cloud_backup-5", "Tap@cloud_backup-5-2026",
                        list_snapshots=mw.list_snapshots)

        # ...then the run finishes and cleanup fires, exactly as restic_backup's
        # `finally` does.
        tn.cleanup_task(mw, "cloud_backup-5", list_snapshots=mw.list_snapshots)

        assert mw.snapshots == [], (
            "the zvol/legacy children of an unstaged recursive snapshot were orphaned. "
            "Stock deletes only the parent; if we do not own the sweep, nothing does."
        )

    def test_apply_sh_owns_the_snapshot_on_the_not_nested_path(self):
        # The gate itself. It used to `return snapshot, snap_path` and hand the
        # snapshot back to stock, whose delete is non-recursive.
        with open(os.path.join(os.path.dirname(__file__), "..", "patch", "apply.sh"),
                  encoding="utf-8") as fh:
            src = fh.read()

        gate = src.index("if not nested:")
        ret = src.index("return snapshot, snap_path", gate)
        assert "_tc_nested.own_snapshot(" in src[gate:ret], (
            "the not-nested path returns to stock without recording the snapshot. On "
            "26 stock may have taken a RECURSIVE snapshot (its rule is statfs-based, "
            "not ours) and deletes only the parent -- so every child is orphaned, with "
            "no sidecar and no GC, on every run."
        )


class TestASilentNoOpDeleteCannotDropTheSidecar:
    """A delete that returns without raising is not proof anything was destroyed.

    iX has already gutted `pool.snapshot.do_update` on master into a no-op whose body
    is commented out and which returns None. An AST check still sees the `def`; a
    callable check still sees the method. If `do_delete` ever goes the same way, the
    recursive delete returns cleanly, `delete_snapshot_tree` reports no survivors,
    `cleanup_task` removes the sidecar -- the only record -- and ~250 snapshots are
    orphaned forever with the backup reporting SUCCESS.
    """

    def test_a_delete_that_does_nothing_is_caught_and_reported(self):
        class NoOpDelete(FakeMiddleware):
            def call_sync(self, method, *args):
                self.calls.append((method, args))
                return None            # "succeeds", destroys nothing

        mw = NoOpDelete(["Tap@snap", "Tap/apps@snap", "Tap/apps/lidarr@snap"])
        survivors = tn.delete_snapshot_tree(
            mw, "Tap@snap", list_snapshots=mw.list_snapshots, sleep=lambda _s: None,
        )

        assert sorted(survivors) == sorted(
            ["Tap@snap", "Tap/apps@snap", "Tap/apps/lidarr@snap"]), (
            "a no-op delete must be REPORTED as survivors, so cleanup_task keeps the "
            "sidecar and the next run reclaims them. Returning [] here silently "
            "orphans the entire tree."
        )

    def test_an_unconfirmable_delete_keeps_owning_the_tree(self):
        # If ZFS cannot be read we cannot confirm the delete did anything. Claiming a
        # clean sweep makes cleanup_task DROP the sidecar -- and if the delete had in
        # fact done nothing, the tree is orphaned with no record of it, forever.
        #
        # The two mistakes are not symmetric. A false survivor self-heals: the sidecar
        # is kept, the next run reclaims it, the delete raises "does not exist", and
        # the record clears. A lost record is permanent. So when in doubt, keep owning.
        mw = FakeMiddleware(["Tap@snap"])

        def cannot_enumerate(_dataset):
            raise tn.ZfsError("pool I/O is currently suspended")

        assert tn.delete_snapshot_tree(
            mw, "Tap@snap", list_snapshots=cannot_enumerate, sleep=lambda _s: None,
        ) == ["Tap@snap"]


class TestTheMalformedRowGuard:
    """`zfs list -H` neither quotes nor escapes. A tab in a mountpoint splits wrong.

    Dropping such a row would remove a dataset from the staging plan without it
    appearing in `skipped` either -- the cardinal-rule failure, on the newest code
    path. Two mutations (silently filtering the row; dropping the `fields=` argument)
    used to pass the whole suite.
    """

    @staticmethod
    def _runner(stdout):
        class R:
            returncode = 0
            stderr = ""
        R.stdout = stdout
        return lambda cmd: R()

    def test_a_row_with_the_wrong_field_count_RAISES(self):
        # A mountpoint containing a tab -> 4 fields, not 3.
        bad = "scratch\t/mnt/scratch\tyes\nscratch/odd\t/mnt/od\td\tyes\n"
        with pytest.raises(tn.ZfsError, match="tab-separated"):
            tn.query_filesystems(runner=self._runner(bad))

    def test_the_error_names_the_offending_row(self):
        bad = "a\tb\tc\nbroken\trow\n"
        with pytest.raises(tn.ZfsError, match="broken"):
            tn.query_filesystems(runner=self._runner(bad))

    def test_the_field_count_is_actually_enforced_for_snapshots_too(self):
        with pytest.raises(tn.ZfsError):
            tn.list_snapshot_names("Tap", runner=self._runner("ok\nnot\tok\n"))

    def test_query_filesystems_asks_zfs_for_filesystems_only(self):
        # Dropping `-t filesystem` would drag in volumes and snapshots, which the
        # planner would then try to stage.
        seen = []

        def runner(cmd):
            seen.append(cmd)
            return self._runner("Tap\t/mnt/Tap\tyes\n")(cmd)

        tn.query_filesystems(runner=runner)
        assert "-t" in seen[0] and "filesystem" in seen[0]
        assert "name,mountpoint,mounted" in seen[0]


class TestTheGarbageCollectorIsActuallyWiredIn:
    """The GC is the ONLY recovery path when the sidecar itself is gone.

    The sidecar lives in /run (tmpfs), so a reboot mid-backup destroys it and orphans
    the whole tree with nothing pointing at it. `own_snapshot` is the GC's only
    production caller -- and stubbing that call out used to pass all 304 tests, i.e.
    the GC could have been silently disconnected.
    """

    def test_own_snapshot_runs_the_collector(self, tmp_path, monkeypatch):
        monkeypatch.setattr(tn, "STAGING_BASE", str(tmp_path))
        ran = []
        monkeypatch.setattr(
            tn, "gc_stale_snapshots",
            lambda *a, **kw: ran.append(a[1]) or [],
        )
        mw = FakeMiddleware(["Tap@new"])
        tn.own_snapshot(mw, "cloud_backup-5", "Tap@new",
                        list_snapshots=mw.list_snapshots)
        assert ran == ["cloud_backup-5"], (
            "own_snapshot did not run the garbage collector. It is the only thing that "
            "ever finds a tree whose sidecar was lost to a reboot."
        )

    def test_a_collected_orphan_is_carried_into_the_sidecar_if_it_survives(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(tn, "STAGING_BASE", str(tmp_path))
        monkeypatch.setattr(
            tn, "gc_stale_snapshots", lambda *a, **kw: ["Tap/x@busy-orphan"],
        )
        mw = FakeMiddleware(["Tap@new"])
        root = tn.own_snapshot(mw, "cloud_backup-5", "Tap@new",
                               list_snapshots=mw.list_snapshots)
        assert "Tap/x@busy-orphan" in tn._read_sidecar(root), (
            "an orphan the GC could not delete must be RECORDED, or the next run has "
            "no idea it exists"
        )


class TestTheServiceIsResolvedLazily:
    """`_Snapshots.service` resolves on first use, not in the constructor.

    Eager resolution looks harmless and is not: `gc_stale_snapshots` would raise inside
    its own broad `except` and silently collect nothing, and `delete_snapshot_tree`
    would raise `StagingError` out of the constructor -- outside its try -- instead of
    returning survivors.
    """

    def test_constructing_it_against_a_hopeless_middleware_does_not_raise(self):
        class Neither(FakeMiddleware):
            def get_service(self, name):
                raise KeyError(name)

        snaps = tn._Snapshots(Neither())          # must not raise
        assert snaps.names is not None

        with pytest.raises(StagingError):
            snaps.delete("Tap@snap")              # ...only the MUTATION refuses
