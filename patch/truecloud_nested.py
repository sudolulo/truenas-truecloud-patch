"""Nested-dataset snapshot support for TrueCloud Backup / Cloud Sync.

Why this exists
---------------
Stock TrueNAS refuses ``snapshot = true`` when the backup path contains child
datasets::

    This option is only available for datasets that have no further nesting

That guard is *correct* and it is not laziness. ``plugins/cloud/snapshot.py``
already takes a **recursive** ZFS snapshot, but it then points the backup tool
at the *parent* dataset's ``.zfs/snapshot/<snap>/`` directory -- and ZFS does
not expose child datasets through a parent's snapshot directory::

    /mnt/Tap/.zfs/snapshot/<snap>/apps/   ->  0 entries   (children invisible)
    /mnt/Tap/apps/lidarr/config/.zfs/snapshot/<snap>/  ->  the real data

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

Notes
-----
* ZFS snapshots are immutable, so a plain ``mount --bind`` is inherently
  read-only; no remount dance is needed.
* Bind-mounting ``.zfs/snapshot/<snap>`` pins the snapshot, so ``zfs destroy``
  of that snapshot returns EBUSY until we unmount. Stock ``restic_backup()``
  deletes the snapshot in its ``finally``, which therefore logs one benign
  "Error deleting snapshot ... busy" warning; :func:`cleanup_task` then unmounts
  and deletes the snapshot for real. See ``patch/apply.sh``.
* Staging roots live under a stable, per-task path so that the backup tool sees
  the *same* path every run. Stock's ``.zfs/snapshot/<name>-<timestamp>/`` path
  changes every run, which defeats restic's parent-snapshot detection; the
  staging tree is an improvement on that.
"""

from __future__ import annotations

import contextlib
import os
import subprocess

__all__ = [
    "StagingError",
    "STAGING_BASE",
    "ACTIVE",
    "staging_root_for",
    "plan_staging",
    "current_mounts_under",
    "apply_plan",
    "verify_staged",
    "teardown",
]

#: Where staging trees are assembled. tmpfs; bind mounts consume no space.
STAGING_BASE = "/run/truecloud-nested"

#: staging_root -> zfs snapshot name ("pool/ds@snap"), for cleanup.
ACTIVE: dict[str, str] = {}


class StagingError(Exception):
    """Staging could not produce a complete tree. The backup must not proceed."""


def staging_root_for(name: str, base: str = STAGING_BASE) -> str:
    """Stable staging root for a task name (e.g. ``cloud_backup-5``)."""
    safe = "".join(c if (c.isalnum() or c in "-_.") else "_" for c in name) or "task"
    return os.path.join(base, safe)


def _depth(path: str) -> int:
    return len([p for p in path.split("/") if p])


def plan_staging(base_mountpoint, path, snapshot_name, datasets, staging_root,
                 isdir=os.path.isdir):
    """Compute the bind-mount plan for staging a nested tree. Pure function.

    ``datasets`` is a list of dicts shaped like ``zfs.dataset.query`` results:
    ``{"name": str, "properties": {"mountpoint": {"value": str},
    "mounted": {"value": "yes"|"no"}}}``.

    Returns ``(mounts, skipped)`` where ``mounts`` is an ordered list of
    ``(source, target)`` pairs (parents before children) and ``skipped`` is a
    list of ``(dataset_name, reason)``.

    Raises StagingError if a descendant holds data we would silently omit.
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

    prefix = path.rstrip("/") + "/"
    for ds in datasets:
        props = ds.get("properties", {})
        mp = props.get("mountpoint", {}).get("value", "")
        name = ds.get("name", "?")

        if not mp or mp in ("none", "legacy", "-"):
            skipped.append((name, f"mountpoint is {mp or 'unset'}"))
            continue
        if not mp.startswith(prefix):
            continue  # not a descendant of the backup path

        mounted = props.get("mounted", {}).get("value", "yes")
        if mounted == "no":
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

        target = os.path.join(staging_root, os.path.relpath(mp, path))
        mounts.append((src, target))

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


def _run(cmd):
    return subprocess.run(cmd, capture_output=True, text=True, check=False)


def apply_plan(mounts, runner=_run):
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
            if not os.path.isdir(target):
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


def verify_staged(mounts, runner=_run, ismount=os.path.ismount, listdir=os.listdir):
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


async def stage_nested(middleware, path, snapshot, base_mountpoint, task_name, logger=None):
    """Build a complete staging tree for `path` from the already-taken `snapshot`.

    `snapshot` is a full ZFS snapshot name ("Tap@cloud_backup-5-2026...").
    Returns the staging root to hand to the backup tool.

    Raises StagingError if the tree cannot be staged completely -- the caller
    must let that propagate so the backup fails instead of silently uploading a
    partial tree.
    """
    snapshot_name = snapshot.split("@", 1)[1]
    staging_root = staging_root_for(task_name)

    # A previous run may have crashed mid-flight; never build on top of that.
    await middleware.run_in_thread(teardown, staging_root)

    datasets = await middleware.call("zfs.dataset.query", [["type", "=", "FILESYSTEM"]])

    mounts, skipped = await middleware.run_in_thread(
        plan_staging, base_mountpoint, path, snapshot_name, datasets, staging_root
    )
    if logger:
        for name, reason in skipped:
            logger.warning("truecloud-patch: not staging dataset %r: %s", name, reason)

    await middleware.run_in_thread(apply_plan, mounts)
    try:
        await middleware.run_in_thread(verify_staged, mounts)
    except Exception:
        await middleware.run_in_thread(teardown, staging_root)
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

    Stock `restic_backup()` deletes the snapshot in its own `finally`, which
    fails with EBUSY while our bind mounts pin it (it logs a warning and moves
    on). We unmount here and then delete the snapshot for real.
    """
    staging_root = staging_root_for(task_name)
    snapshot = ACTIVE.pop(staging_root, None)

    if snapshot is None and not os.path.isdir(staging_root):
        return  # never staged; nothing to do

    errors = await middleware.run_in_thread(teardown, staging_root)
    if errors and logger:
        for err in errors:
            logger.warning("truecloud-patch: staging teardown: %s", err)

    if snapshot is not None:
        try:
            await middleware.call("zfs.snapshot.delete", snapshot)
        except Exception as e:  # noqa: BLE001 - cleanup must never mask the real error
            if logger:
                logger.warning(
                    "truecloud-patch: could not delete snapshot %s: %r", snapshot, e
                )
