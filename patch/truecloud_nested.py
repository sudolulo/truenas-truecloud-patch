"""Nested-dataset snapshot support for TrueCloud Backup.

Why this exists
---------------
Stock TrueNAS refuses ``snapshot = true`` when the backup path contains child
datasets::

    This option is only available for datasets that have no further nesting

That guard is *correct* and it is not laziness. ``plugins/cloud/snapshot.py``
already takes a **recursive** ZFS snapshot, but it then points the backup tool
at the *parent* dataset's ``.zfs/snapshot/<snap>/`` directory -- and ZFS does
not expose child datasets through a parent's snapshot directory::

    /mnt/Tap/.zfs/snapshot/<snap>/apps/                 ->  0 entries
    /mnt/Tap/apps/lidarr/config/.zfs/snapshot/<snap>/   ->  the real data

So without the guard, the backup tool would walk a near-empty tree, report
SUCCESS, and upload almost nothing. A backup that lies about succeeding is the
worst failure a backup system can have, so middleware refuses the config
instead.

This module implements the missing half: after the (already recursive) snapshot
is taken, every descendant dataset's *own* ``.zfs/snapshot/<snap>`` directory is
bind-mounted into a staging tree that mirrors the original layout. The backup
tool is then pointed at the staging root, which is a complete, consistent,
point-in-time view of the whole subtree.

Cardinal safety rule
--------------------
**If the tree cannot be staged completely, fail loudly.** Never return a partial
tree. Silently backing up an incomplete tree is precisely the failure this
feature exists to prevent, and it would be worse than not having the feature.

Snapshot lifecycle -- read this before changing anything
--------------------------------------------------------
``zfs.snapshot.delete`` defaults to ``recursive=False``, and stock
``restic_backup()`` calls it with no options. Stock gets away with that because
its validation means ``recursive`` is never actually True in the field. Enabling
nested datasets makes recursive snapshots real, so the parent
(``Tap@snap``) has one child snapshot per descendant dataset (160+ here).
Deleting only the parent would orphan every child on **every successful run**.

Therefore this module owns the whole lifecycle:

* :func:`delete_snapshot_tree` sweeps the parent *and* every child snapshot, and
  is idempotent -- it copes with stock's ``finally`` having already removed the
  parent.
* The snapshot name is recorded in a sidecar file next to the staging root, not
  only in memory, so a middlewared restart mid-backup cannot orphan it.
* Bind-mounting ``.zfs/snapshot/<snap>`` pins the snapshot, so stock's delete
  fails with EBUSY and logs one benign warning; we unmount and then sweep.
"""

from __future__ import annotations

import contextlib
import os
import subprocess

__all__ = [
    "ACTIVE",
    "STAGING_BASE",
    "StagingError",
    "apply_plan",
    "cleanup_task",
    "current_mounts_under",
    "delete_snapshot_tree",
    "plan_staging",
    "sidecar_for",
    "snapshot_tree_names",
    "stage_nested",
    "staging_root_for",
    "teardown",
    "verify_staged",
]

#: Where staging trees are assembled. tmpfs; bind mounts consume no space.
STAGING_BASE = "/run/truecloud-nested"

#: staging_root -> zfs snapshot name. A cache; the sidecar file is the source of
#: truth, so that a middlewared restart cannot orphan a snapshot.
ACTIVE: dict[str, str] = {}


class StagingError(Exception):
    """Staging could not produce a complete tree. The backup must not proceed."""


# ── pure helpers ──────────────────────────────────────────────────────────────


def staging_root_for(name: str, base: str | None = None) -> str:
    """Stable staging root for a task name (e.g. ``cloud_backup-5``).

    ``base`` defaults to :data:`STAGING_BASE` at CALL time, not at import time --
    a ``base=STAGING_BASE`` default would freeze the value into the function
    object and silently ignore any later override.
    """
    if base is None:
        base = STAGING_BASE
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name)
    # A component of "." or ".." would escape STAGING_BASE once joined.
    if not safe or safe.strip(".") == "":
        safe = "task"
    return os.path.join(base, safe)


def sidecar_for(staging_root: str) -> str:
    """Path of the file recording which ZFS snapshot a staging tree pins."""
    return staging_root + ".snapshot"


def _write_sidecar(staging_root: str, snapshot: str) -> None:
    """Record the pinned snapshot on disk. Blocking; call via run_in_thread."""
    with contextlib.suppress(OSError):
        os.makedirs(os.path.dirname(staging_root), exist_ok=True)
        with open(sidecar_for(staging_root), "w", encoding="utf-8") as fh:
            fh.write(snapshot)


def _read_sidecar(staging_root: str) -> str | None:
    """The snapshot a previous run recorded here, if any."""
    try:
        with open(sidecar_for(staging_root), encoding="utf-8") as fh:
            return fh.read().strip() or None
    except OSError:
        return None


def _remove_sidecar(staging_root: str) -> None:
    with contextlib.suppress(OSError):
        os.unlink(sidecar_for(staging_root))


def _depth(path: str) -> int:
    return len([p for p in path.split("/") if p])


def snapshot_tree_names(snapshot: str, all_names) -> list[str]:
    """Every snapshot produced by ``zfs snapshot -r <dataset>@<snap>``.

    That is the parent plus one per descendant dataset, all sharing the same
    name after the ``@``. Pure, so the sweep logic is testable without ZFS.
    """
    dataset, _, snapname = snapshot.partition("@")
    if not snapname:
        return []
    parent = f"{dataset}@{snapname}"
    prefix = dataset + "/"
    suffix = "@" + snapname
    return [
        n for n in all_names
        if n == parent or (n.startswith(prefix) and n.endswith(suffix))
    ]


def plan_staging(base_dataset, base_mountpoint, path, snapshot_name, datasets,
                 staging_root, isdir=os.path.isdir):
    """Compute the bind-mount plan for staging a nested tree. Pure function.

    ``datasets`` is a list of dicts shaped like ``zfs.dataset.query`` results:
    ``{"name": str, "properties": {"mountpoint": {"value": str},
    "mounted": {"value": "yes"|"no"}}}``.

    Returns ``(mounts, skipped)`` where ``mounts`` is an ordered list of
    ``(source, target)`` pairs (parents before children) and ``skipped`` is a
    list of ``(dataset_name, reason)`` covering only datasets that are *in
    scope* -- i.e. descendants of ``base_dataset``. Datasets elsewhere on the
    system are ignored silently; reporting them would bury the ones that matter.

    Raises StagingError if an in-scope descendant holds data we would otherwise
    silently omit.
    """
    def snapdir(mountpoint):
        return os.path.join(mountpoint, ".zfs", "snapshot", snapshot_name)

    # Root of the staging tree: the backup path as seen inside the base
    # dataset's own snapshot.
    rel = os.path.relpath(path, base_mountpoint)
    root_src = snapdir(base_mountpoint)
    if rel != ".":
        root_src = os.path.join(root_src, rel)

    mounts = [(root_src, staging_root)]
    skipped = []

    ds_prefix = base_dataset.rstrip("/") + "/"
    path_prefix = path.rstrip("/") + "/"

    for ds in datasets:
        name = ds.get("name", "")
        # Scope by DATASET NAME, not mountpoint: a dataset with no mountpoint
        # cannot be scoped by path, and scoping by path first would drag in
        # every mountpoint-less dataset on the box (all of Tank/.system/*, ...).
        if not name.startswith(ds_prefix):
            continue

        props = ds.get("properties", {})
        mp = props.get("mountpoint", {}).get("value", "")

        if not mp or mp in ("none", "legacy", "-"):
            skipped.append((name, f"mountpoint is {mp or 'unset'}"))
            continue

        if not mp.startswith(path_prefix):
            # A descendant dataset mounted outside the backed-up path is
            # genuinely not part of this tree. Not an omission.
            continue

        if props.get("mounted", {}).get("value", "yes") == "no":
            # An unmounted (e.g. locked/encrypted) dataset contributes nothing to
            # the live tree either, so skipping matches stock semantics -- but it
            # is a real gap and must be visible, never silent.
            skipped.append((name, "dataset is not mounted (locked/encrypted?)"))
            continue

        src = snapdir(mp)
        if not isdir(src):
            # The recursive snapshot should have covered every descendant. If it
            # did not, this dataset's data would be silently omitted. Refuse.
            raise StagingError(
                f"dataset {name!r} has no snapshot {snapshot_name!r} at {src!r}; "
                f"refusing to back up an incomplete tree"
            )

        mounts.append((src, os.path.join(staging_root, os.path.relpath(mp, path))))

    # Parents before children, so each mountpoint exists before we mount onto it.
    mounts.sort(key=lambda m: _depth(m[1]))
    return mounts, skipped


def current_mounts_under(root, mounts_file="/proc/self/mounts"):
    """Mountpoints at or under ``root``, deepest first. Used for teardown."""
    found = []
    try:
        with open(mounts_file, encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) < 2:
                    continue
                mp = parts[1].replace("\\040", " ").replace("\\011", "\t")
                if mp == root or mp.startswith(root.rstrip("/") + "/"):
                    found.append(mp)
    except OSError:
        return []
    found.sort(key=_depth, reverse=True)
    return found


# ── mount / unmount ───────────────────────────────────────────────────────────


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def apply_plan(mounts, runner=_run, isdir=os.path.isdir):
    """Execute the bind-mount plan. Blocking; call via ``run_in_thread``.

    Raises StagingError on the first failure, after rolling back what was
    mounted -- a half-built tree must never be handed to the backup tool.
    """
    if not mounts:
        raise StagingError("empty staging plan")

    staging_root = mounts[0][1]
    done = []
    try:
        os.makedirs(staging_root, exist_ok=True)
        for src, target in mounts:
            if not isdir(target):
                # Child mountpoint dirs come from the parent snapshot, which is
                # read-only -- we cannot mkdir them. Only the root is ours.
                raise StagingError(f"staging target {target!r} does not exist")
            res = runner(["mount", "--bind", src, target])
            if res.returncode != 0:
                raise StagingError(
                    f"bind-mount {src!r} -> {target!r} failed: "
                    f"{(res.stderr or '').strip() or res.returncode}"
                )
            done.append(target)
    except Exception:
        for target in reversed(done):
            runner(["umount", "-l", target])
        with contextlib.suppress(OSError):
            os.rmdir(staging_root)
        raise
    return staging_root


def verify_staged(mounts, ismount=os.path.ismount, listdir=os.listdir):
    """Assert the staged tree is real and complete. Raises StagingError if not.

    This is the anti-regression guard: it is what stops this feature from ever
    degrading back into the silently-empty backup that the stock validation
    refuses to allow.
    """
    if not mounts:
        raise StagingError("nothing was staged")

    staging_root = mounts[0][1]
    for _src, target in mounts:
        if not ismount(target):
            raise StagingError(f"staging target {target!r} is not a mountpoint")

    try:
        if not listdir(staging_root):
            raise StagingError(f"staging root {staging_root!r} is empty")
    except OSError as e:
        raise StagingError(f"staging root {staging_root!r} unreadable: {e}") from e

    return True


def teardown(staging_root, runner=_run, mounts_file="/proc/self/mounts"):
    """Unmount the staging tree (deepest first) and remove the root.

    Idempotent, and does not depend on an in-memory plan -- so it also cleans up
    leftovers from a crashed run.
    """
    errors = []
    for mp in current_mounts_under(staging_root, mounts_file=mounts_file):
        res = runner(["umount", mp])
        if res.returncode != 0:
            res = runner(["umount", "-l", mp])  # lazy: better than leaking
            if res.returncode != 0:
                errors.append(f"{mp}: {(res.stderr or '').strip()}")
    with contextlib.suppress(OSError):
        os.rmdir(staging_root)
    return errors


# ── async orchestration (middleware is duck-typed; no middlewared import) ─────


async def delete_snapshot_tree(middleware, snapshot, logger=None):
    """Delete the parent snapshot AND every child created by ``zfs snapshot -r``.

    ``zfs.snapshot.delete`` is non-recursive by default and stock calls it with
    no options, so relying on stock would orphan one snapshot per descendant
    dataset on every run. Idempotent: tolerates the parent already being gone
    (stock's ``finally`` may have won the race once our mounts were released).
    """
    dataset = snapshot.partition("@")[0]

    try:
        snaps = await middleware.call(
            "zfs.snapshot.query", [["name", "^", dataset]], {"select": ["name"]}
        )
        # An empty result means the tree is already gone -- delete nothing, and
        # do not fall back to the parent, which would only log a spurious
        # "does not exist" warning on every clean run.
        names = snapshot_tree_names(snapshot, [s["name"] for s in snaps])
    except Exception as e:  # noqa: BLE001 - fall back to at least the parent
        if logger:
            logger.warning(
                "truecloud-patch: could not enumerate snapshot tree for %s: %r",
                snapshot, e,
            )
        names = [snapshot]

    for name in names:
        try:
            await middleware.call("zfs.snapshot.delete", name)
        except Exception as e:  # noqa: BLE001 - already gone is fine
            if logger:
                logger.warning(
                    "truecloud-patch: could not delete snapshot %s: %r", name, e
                )


async def stage_nested(middleware, path, snapshot, base_dataset, base_mountpoint,
                       task_name, datasets, logger=None):
    """Build a complete staging tree for `path` from the already-taken `snapshot`.

    `snapshot` is a full ZFS snapshot name ("Tap@cloud_backup-5-2026...").

    `datasets` is the FILESYSTEM dataset list. **It MUST have been enumerated
    AFTER `snapshot` was taken.** A list read beforehand can miss a dataset
    created in the gap: the recursive snapshot would capture it, but the staging
    plan would not, and its data would be silently omitted from the backup.
    Enumerated afterwards, an unsnapshotted dataset instead trips the isdir()
    check in plan_staging and fails the run loudly.

    Returns the staging root to hand to the backup tool.

    Raises StagingError if the tree cannot be staged completely -- the caller
    must let that propagate so the backup fails instead of silently uploading a
    partial tree. The caller is responsible for deleting `snapshot` in that case
    (see SNAPSHOT_BLOCK in apply.sh).
    """
    snapshot_name = snapshot.split("@", 1)[1]
    staging_root = staging_root_for(task_name)

    # A previous run may have crashed mid-flight; never build on top of that.
    await middleware.run_in_thread(teardown, staging_root)

    # ...and if it left a sidecar behind, that snapshot tree is still on disk and
    # nothing else will ever reclaim it. Sweep it before we overwrite the record,
    # or a single crashed run orphans 160+ snapshots permanently.
    stale = await middleware.run_in_thread(_read_sidecar, staging_root)
    if stale and stale != snapshot:
        if logger:
            logger.warning(
                "truecloud-patch: reclaiming snapshot tree from an earlier "
                "interrupted run: %s", stale,
            )
        await delete_snapshot_tree(middleware, stale, logger=logger)

    # Record the snapshot BEFORE mounting anything, not after. middlewared can
    # die at any point (this patch even schedules a restart at boot), and the
    # sidecar is the only thing that survives it -- an in-process dict would take
    # the sole record of a 160-snapshot tree with it. Writing it after apply_plan
    # would leave exactly the crash window the sidecar exists to close.
    await middleware.run_in_thread(_write_sidecar, staging_root, snapshot)

    try:
        mounts, skipped = await middleware.run_in_thread(
            plan_staging, base_dataset, base_mountpoint, path, snapshot_name,
            datasets, staging_root,
        )
        if logger:
            for name, reason in skipped:
                logger.warning(
                    "truecloud-patch: not staging dataset %r: %s", name, reason
                )

        await middleware.run_in_thread(apply_plan, mounts)
        await middleware.run_in_thread(verify_staged, mounts)
    except Exception:
        await middleware.run_in_thread(teardown, staging_root)
        await middleware.run_in_thread(_remove_sidecar, staging_root)
        raise

    ACTIVE[staging_root] = snapshot

    if logger:
        logger.info(
            "truecloud-patch: staged %d dataset(s) from %s at %s",
            len(mounts), snapshot, staging_root,
        )
    return staging_root


async def cleanup_task(middleware, task_name, logger=None):
    """Tear down a task's staging tree and delete the snapshot it pinned.

    Safe to call unconditionally: a no-op when the task was never staged.
    """
    staging_root = staging_root_for(task_name)
    sidecar = sidecar_for(staging_root)

    snapshot = ACTIVE.pop(staging_root, None)
    if snapshot is None:
        # Sidecar survives a middlewared restart; ACTIVE does not.
        snapshot = _read_sidecar(staging_root)

    if snapshot is None and not os.path.isdir(staging_root):
        return  # never staged; nothing to do

    errors = await middleware.run_in_thread(teardown, staging_root)
    if errors and logger:
        for err in errors:
            logger.warning("truecloud-patch: staging teardown: %s", err)

    if snapshot is not None:
        await delete_snapshot_tree(middleware, snapshot, logger=logger)

    with contextlib.suppress(OSError):
        os.unlink(sidecar)
