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
import stat
import subprocess

__all__ = [
    "STAGING_BASE",
    "StagingError",
    "apply_plan",
    "cleanup_all",
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

# Which snapshot a staging tree pins is recorded ONLY in the sidecar file, never
# also in memory. An in-process dict would be a second source of truth that a
# middlewared restart silently empties -- and it is exactly the restart case that
# must not orphan a 250-snapshot tree. One record, on disk, or none.


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


def _probe_snapdir(path):
    """Classify a snapshot directory: ``ok``, ``missing``, or why it is unusable.

    ``os.path.isdir()`` collapses "does not exist" and "cannot stat" into the
    same ``False``, so an EACCES would report itself as "has no snapshot" and
    send someone hunting for a snapshot that is sitting right there. Both cases
    still abort the backup -- but it has to say which one.
    """
    try:
        st = os.stat(path)
    except FileNotFoundError:
        return "missing"
    except OSError as e:
        return f"cannot be read ({e.strerror})"
    return "ok" if stat.S_ISDIR(st.st_mode) else "is not a directory"


def plan_staging(base_dataset, base_mountpoint, path, snapshot_name, datasets,
                 staging_root, probe=_probe_snapdir):
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
        status = probe(src)
        if status != "ok":
            # Either the recursive snapshot missed this dataset, or we cannot read
            # it. Either way its data would be silently omitted. Refuse -- but say
            # WHICH, because "no snapshot" and "permission denied" send you to
            # completely different places.
            detail = (
                f"has no snapshot {snapshot_name!r}" if status == "missing"
                else f"snapshot {snapshot_name!r} {status}"
            )
            raise StagingError(
                f"dataset {name!r} {detail} at {src!r}; "
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

    # Fast path: ONE recursive delete removes the parent and every child that
    # `zfs snapshot -r` created (252 on a real pool). Deleting them individually
    # also works, but it is neither cheap nor atomic -- a run killed part-way
    # through 252 sequential deletes leaves exactly the orphans this function
    # exists to prevent.
    try:
        await middleware.call("zfs.snapshot.delete", snapshot, {"recursive": True})
        return
    except Exception:  # noqa: BLE001 - fall through to the explicit sweep
        pass

    # The parent may already be gone -- stock's `finally` can win the race once
    # our mounts are released -- which fails the recursive delete while the
    # children survive. Sweep them by name.
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
    snapshot = _read_sidecar(staging_root)

    if snapshot is None and not os.path.isdir(staging_root):
        return  # never staged; nothing to do

    errors = await middleware.run_in_thread(teardown, staging_root)
    if errors and logger:
        for err in errors:
            logger.warning("truecloud-patch: staging teardown: %s", err)

    if snapshot is not None:
        await delete_snapshot_tree(middleware, snapshot, logger=logger)

    _remove_sidecar(staging_root)


# ── offline cleanup (uninstall.sh / recover.sh) ───────────────────────────────


def cleanup_all(base=None, runner=_run, mounts_file="/proc/self/mounts",
                glob_fn=None, read_sidecar=_read_sidecar):
    """Tear down every staging tree. Used by uninstall.sh and recover.sh.

    Those scripts must work when middlewared is dead, so they cannot go through
    the async path -- but they must not reimplement the teardown either: the
    depth-ordering and lazy-umount fallback are fiddly, and a second copy in
    shell would be the untested one. This is the same tested code.

    Returns ``(lines, errors)``: report lines to print, and unmount errors.
    """
    import glob as _glob

    base = base or STAGING_BASE
    glob_fn = glob_fn or _glob.glob
    lines = []

    # Report orphaned snapshots BEFORE removing the sidecars that name them --
    # a sidecar is the only record that an interrupted run's snapshot tree (one
    # snapshot per descendant dataset) is still on disk.
    for sc in sorted(glob_fn(os.path.join(base, "*.snapshot"))):
        snap = read_sidecar(sc[: -len(".snapshot")])
        if snap:
            lines.append(f"  NOTE: an interrupted backup left snapshot '{snap}' behind.")
            lines.append(f"        Remove it and its children:  zfs destroy -r '{snap}'")

    mounts = current_mounts_under(base, mounts_file=mounts_file)
    if not mounts:
        lines.append("  None active.")
    for mp in mounts:
        lines.append(f"  Unmounting: {mp}")

    errors = teardown(base, runner=runner, mounts_file=mounts_file)
    for err in errors:
        lines.append(f"  WARNING: could not unmount {err}")

    if not errors:
        for sc in glob_fn(os.path.join(base, "*.snapshot")):
            with contextlib.suppress(OSError):
                os.unlink(sc)
        with contextlib.suppress(OSError):
            os.rmdir(base)

    return lines, errors


if __name__ == "__main__":
    import sys

    if len(sys.argv) > 1 and sys.argv[1] == "cleanup":
        _lines, _errors = cleanup_all()
        for _line in _lines:
            print(_line)
        sys.exit(1 if _errors else 0)
    print("usage: truecloud_nested.py cleanup", file=sys.stderr)
    sys.exit(2)
