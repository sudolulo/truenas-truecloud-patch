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
The snapshot delete call defaults to ``recursive=False``, and stock
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
import datetime
import os
import stat
import subprocess
import time

__all__ = [
    "DELETE_METHODS",
    "SNAPSHOT_SERVICES",
    "STAGING_BASE",
    "StagingError",
    "ZfsError",
    "apply_plan",
    "cleanup_all",
    "cleanup_task",
    "current_mounts_under",
    "delete_snapshot_tree",
    "gc_stale_snapshots",
    "list_snapshot_names",
    "mounted_snapshots",
    "own_snapshot",
    "pick_snapshot_service",
    "plan_staging",
    "query_filesystems",
    "sidecar_for",
    "snapshot_service",
    "snapshot_tree_names",
    "stage_nested",
    "stale_snapshot_names",
    "staging_root_for",
    "teardown",
    "verify_staged",
]


# ── how this module talks to the system ──────────────────────────────────────
#
#   READ the truth from ZFS.  MAKE CHANGES through middleware.
#
# That split is not stylistic. It was forced by finding, on a real TrueNAS 26 box,
# that middleware's query APIs apply a VISIBILITY POLICY:
#
#   zfs list                 274 datasets      205 from pool.dataset.query
#   zfs list -t snapshot     274 snapshots     205 from pool.snapshot.query
#
# The missing 69 are the datasets TrueNAS considers its own -- `ix-apps/*`,
# `.system/*`, `.ix-virt/*` -- and on the real pool that is 84 of 270, including
# `ix-apps`, which holds live application data. Enumerating from that view would
# have silently omitted every one of them from the staging plan and from the
# snapshot sweep: a green backup missing data, and one orphaned snapshot per
# hidden dataset on every run. Both are exactly what this module exists to
# prevent.
#
# This went unnoticed because the patch used to call the PRIVATE `zfs.dataset.query`
# and `zfs.snapshot.query`, which return everything. TrueNAS 26 deleted them, and
# the public replacements are NOT like-for-like -- they are filtered. So
# enumeration now reads ZFS directly, which no policy can filter and which behaves
# identically on every release.
#
# MUTATION still goes through middleware, so TrueNAS's own bookkeeping stays
# consistent -- and an exact-name delete works fine even on a dataset the query
# hides. The one wrinkle is that no single snapshot namespace spans every
# supported release, so it is resolved at runtime rather than pinned:
#
#   24.10, 25.04   `zfs.snapshot`  (public back then; `pool.snapshot` does not exist)
#   25.10          both -- `pool.snapshot` public, `zfs.snapshot` demoted to private
#   26             `pool.snapshot` only -- `plugins/zfs_/` is gone
#
#: Snapshot CRUD namespaces, best first. `tools/compat.py` checks this exact list
#: (MiddlewareCall.also) with the same predicate the runtime uses -- the namespace
#: exists AND it defines `delete`/`do_delete` -- and a test binds the two lists
#: together, so what CI verifies and what runs cannot drift apart.
SNAPSHOT_SERVICES = ("pool.snapshot", "zfs.snapshot")


#: The CRUDService method spellings that answer to `<namespace>.delete`. A
#: CRUDService exposes `delete` from a method NAMED `do_delete`; both are live
#: across the matrix. `tools/compat.py` accepts exactly this pair.
DELETE_METHODS = ("delete", "do_delete")


def pick_snapshot_service(can_delete):
    """First namespace in SNAPSHOT_SERVICES that can actually DELETE for us.

    Pure: `can_delete(namespace) -> bool`. Returns None if no namespace can,
    which is a middleware we have never seen and must not guess about.

    The predicate is "can delete", NOT "the service is registered", and the
    difference is the whole point. `get_service()` only proves the namespace is
    in the registry; it says nothing about whether `delete` still exists on it.
    `tools/compat.py` checks namespace AND method, so if the runtime settled for
    the weaker test the two could disagree — and would, in the one way that
    matters: iX guts a method while keeping its service (they have already done
    exactly that to `pool.snapshot.do_update` on master). compat would try
    `pool.snapshot`, find `delete` gone, fall through to `zfs.snapshot`, and
    report **ok**; the runtime would take `pool.snapshot` because the service is
    still registered, and then fail on every single delete — orphaning the whole
    tree while the backup reports success.

    Same predicate on both sides, so they cannot drift.
    """
    for name in SNAPSHOT_SERVICES:
        if can_delete(name):
            return name
    return None


def _can_delete(middleware, namespace):
    """Is `<namespace>.delete` actually callable on this middleware?"""
    try:
        service = middleware.get_service(namespace)
    except Exception:
        # KeyError for an unregistered namespace; AttributeError if `get_service`
        # itself ever goes away. Both mean "cannot use it", and guessing YES on a
        # service that is not really there fails later, mid-backup, holding a
        # snapshot -- the worst possible moment to find out.
        return False
    return any(callable(getattr(service, m, None)) for m in DELETE_METHODS)


def snapshot_service(middleware):
    """The snapshot namespace this middleware can actually delete through."""
    name = pick_snapshot_service(lambda n: _can_delete(middleware, n))
    if name is None:
        raise StagingError(
            "middleware exposes no usable snapshot delete ("
            + " / ".join(f"{n}.delete" for n in SNAPSHOT_SERVICES)
            + "). Refusing to stage a nested backup, because the snapshot it "
            "creates could not then be swept."
        )
    return name


class _Snapshots:
    """This module's entire interface to snapshots, in one object.

    "READ the truth from ZFS, MAKE CHANGES through middleware" is the rule the whole
    module rests on. It used to live in a comment, while `middleware` and the ZFS
    reader were threaded through five functions **as a pair** -- and the namespace was
    re-resolved in each of them. That is one collaborator, not two, so it is one
    object; the rule is now structural rather than remembered.

    Deliberately private and constructed inside the public functions: `apply.sh`
    injects calls to those functions into middlewared itself, so their signatures are
    a boot-time contract with a live NAS and are not worth churning for tidiness.
    """

    def __init__(self, middleware, list_snapshots=None):
        self._mw = middleware
        self._list = list_snapshots or list_snapshot_names
        self._service = None

    @property
    def service(self):
        """The namespace we delete through. Resolved once, on first use.

        Lazy on purpose: resolving it raises when middleware has no usable delete,
        and the read-only paths must not blow up over a mutation they never make.
        """
        if self._service is None:
            self._service = snapshot_service(self._mw)
        return self._service

    def names(self, dataset):
        """Every snapshot at or under `dataset`, from ZFS. Raises if it cannot be read.

        Never from middleware: its snapshot query hides the internal datasets
        (205 of 274 on the test box), and a snapshot the sweep cannot SEE is a
        snapshot nothing will ever collect.
        """
        return self._list(dataset)

    def delete(self, name, recursive=False):
        """Delete one snapshot -- through middleware, so its bookkeeping stays right.

        An exact-name delete works even on a dataset the query hides; it is only
        enumeration that lies.
        """
        options = ({"recursive": True},) if recursive else ()
        return self._mw.call_sync(f"{self.service}.delete", name, *options)


class ZfsError(Exception):
    """`zfs list` failed. Enumeration is unreliable, so the caller must not guess."""


def _zfs_lines(args, runner=None, fields=None):
    """`zfs <args>` as a list of tab-split rows. Raises ZfsError if it fails.

    Never returns a partial or empty list on failure: a caller that cannot tell
    "no datasets" from "the command broke" will happily stage nothing, or sweep
    nothing, and report success.

    `fields`, if given, is the exact number of tab-separated columns every row must
    have. A row that does not is an ERROR, not something to skip. `zfs list -H`
    neither quotes nor escapes, so a mountpoint containing a tab or a newline would
    split wrong -- and quietly dropping that row would remove a dataset from the
    staging plan without it appearing in `skipped` either. Silent omission is the
    one thing this module may never do, so it raises instead.
    """
    runner = runner or _run
    try:
        r = runner(["zfs", *args])
    except OSError as e:
        # `zfs` missing from middlewared's PATH raises FileNotFoundError, which is
        # not a ZfsError and would sail past callers that only expect one.
        raise ZfsError(f"could not run zfs: {e}") from e

    if r.returncode != 0:
        raise ZfsError((r.stderr or "").strip() or f"zfs {' '.join(args)} failed")

    rows = [ln.split("\t") for ln in r.stdout.splitlines() if ln.strip()]
    if fields is not None:
        bad = [r for r in rows if len(r) != fields]
        if bad:
            raise ZfsError(
                f"zfs {' '.join(args)} returned {len(bad)} row(s) that do not have "
                f"{fields} tab-separated fields (first: {bad[0]!r}). Refusing to "
                f"guess -- a dropped row is a dataset silently missing from the backup."
            )
    return rows


def query_filesystems(middleware=None, runner=None):
    """Every FILESYSTEM dataset, in the shape the planner speaks -- read from ZFS.

    NOT from `pool.dataset.query`, and this is the single most important decision
    in this file.

    middleware's dataset query applies a VISIBILITY POLICY: it hides the datasets
    TrueNAS considers its own -- `ix-apps/*`, `.system/*`, `.ix-virt/*`. That is
    **84 of 270 datasets** on the real pool, and `ix-apps` holds live application
    data. Building the staging plan from that view would silently omit every one of
    them. Worse, `plan_staging()` would never even SEE them, so they would not turn
    up in its `skipped` list either -- no warning, no failure, just a green backup
    quietly missing data. That is exactly the failure this whole module exists to
    prevent, and it is the failure the cardinal rule at the top of this file is
    about.

    It worked before only because the patch called the PRIVATE `zfs.dataset.query`,
    which returned everything. TrueNAS 26 deleted it. The public replacement is not
    a like-for-like: it is a filtered view.

    So: **read the truth from ZFS, make changes through middleware.** ZFS cannot
    apply a policy to what it reports, and `zfs list` behaves identically on every
    release -- which also means one code path instead of a version conditional.

    `middleware` is accepted and ignored, so callers need not care where the data
    comes from.
    """
    rows = _zfs_lines(
        ["list", "-H", "-p", "-o", "name,mountpoint,mounted", "-t", "filesystem"],
        runner=runner, fields=3,
    )
    return [
        {
            "name": name,
            "properties": {
                "mountpoint": {"value": mountpoint},
                # `zfs list` prints yes/no; the planner already speaks that.
                "mounted": {"value": mounted},
            },
        }
        for name, mountpoint, mounted in rows
    ]


def list_snapshot_names(dataset, runner=None):
    """Every snapshot at or under `dataset` -- read from ZFS, for the same reason.

    `pool.snapshot.query` filters exactly like the dataset query does: on this box
    it returned 205 of 274 snapshots, hiding the internal datasets' snapshots. A
    sweep built on that view leaves one orphan per hidden dataset, on every run,
    forever -- which is the bug this module was written to fix in the first place.

    (An EXACT-name delete still works on a hidden dataset, so mutations may keep
    going through middleware. It is only enumeration that lies.)
    """
    rows = _zfs_lines(
        ["list", "-H", "-o", "name", "-t", "snapshot", "-r", dataset],
        runner=runner, fields=1,
    )
    return [r[0] for r in rows]

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


def _write_sidecar(staging_root: str, snapshots) -> None:
    """Record every snapshot tree this task still owns. One per line.

    A LIST, not a single name -- and that is not over-engineering, it is a bug fix.

    The sidecar used to hold one snapshot, so a run that reclaimed an older tree,
    FAILED to finish reclaiming it, and then recorded its own snapshot would
    **overwrite the only record of the survivor** -- orphaning it permanently, which is
    exactly the outcome the sidecar exists to prevent. Observed live: a snapshot
    survived one run, the next run's reclaim also failed (ZFS's 300s automount window
    had not elapsed, because the runs were minutes apart), and the record was
    destroyed anyway.

    Now every still-pending tree is carried forward until it is actually gone.
    """
    if isinstance(snapshots, str):
        snapshots = [snapshots]
    with contextlib.suppress(OSError):
        os.makedirs(os.path.dirname(staging_root), exist_ok=True)
        with open(sidecar_for(staging_root), "w", encoding="utf-8") as fh:
            fh.write("\n".join(dict.fromkeys(snapshots)))   # de-duped, order kept


def _read_sidecar(staging_root: str):
    """Every snapshot tree a previous run recorded here. [] if none.

    Tolerates the old single-line format, which is just a one-element list.
    """
    try:
        with open(sidecar_for(staging_root), encoding="utf-8") as fh:
            return [ln.strip() for ln in fh if ln.strip()]
    except OSError:
        return []


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


#: A snapshot must be at least this old before the garbage collector will touch it.
#:
#: The GC identifies our leftovers by NAME, so its only real risk is deleting a
#: snapshot belonging to a run that is still starting up -- the window between
#: `zfs snapshot -r` and the bind mounts appearing, which is seconds. An hour is three
#: orders of magnitude more slack than that window needs, and still reclaims a lost
#: tree on the very next daily run.
GC_MIN_AGE_SECONDS = 3600


def stale_snapshot_names(task_name, current_snapshot, all_names, now,
                         in_use=(), min_age=GC_MIN_AGE_SECONDS):
    """Snapshots THIS task created in an earlier run and never cleaned up.

    Pure, because this is the one function here that DELETES DATA on a name match, and
    a name match is a weaker claim than a recorded fact. Everything it relies on is an
    argument, so every way it could be wrong is a test.

    Why a garbage collector exists at all, when there is already a sidecar: **the
    sidecar lives in /run, which is tmpfs.** A reboot mid-backup destroys it, and with
    it the only record of a 250-snapshot tree. The sidecar handles the normal case
    precisely; this handles the case where the record itself is gone.

    A snapshot is ours to collect only if ALL of these hold:

      * its name is exactly ``<dataset>@<task_name>-<YYYYMMDDHHMMSS>`` -- so
        ``cloud_backup-5`` never matches ``cloud_backup-50``'s snapshots, and never
        matches a periodic ``auto-2026-…`` or anything a human made;
      * it is not the snapshot the current run is using;
      * nothing is mounted from it (`in_use`) -- an in-flight run pins its own
        snapshots, so this alone protects a concurrent one-time backup;
      * it is older than `min_age` -- which covers the seconds-long window in which a
        run has taken its snapshot but not yet mounted it.

    `now` is a timezone-aware datetime; timestamps in the name are UTC (stock builds
    them with `utc_now()`).
    """
    prefix = task_name + "-"
    stale = []

    for name in all_names:
        _dataset, _, snapname = name.partition("@")
        if not snapname or not snapname.startswith(prefix):
            continue
        if name == current_snapshot or snapname == _snapname_of(current_snapshot):
            continue
        if name in in_use:
            continue

        stamp = snapname[len(prefix):]
        try:
            when = datetime.datetime.strptime(stamp, "%Y%m%d%H%M%S").replace(
                tzinfo=datetime.UTC
            )
        except ValueError:
            # Not our timestamp format. Something else owns this name; leave it alone.
            continue

        if (now - when).total_seconds() < min_age:
            continue

        stale.append(name)

    return stale


def _snapname_of(snapshot):
    return snapshot.partition("@")[2] if snapshot else ""


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

    ``datasets`` is what :func:`query_filesystems` returns:
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
    # List form, never shell=True: `cmd` is built from our own mount plan, so ZFS
    # dataset names cannot inject. Runs as root by definition (it mounts).
    return subprocess.run(  # noqa: S603
        cmd, capture_output=True, text=True, check=False
    )


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


def snapdir_automounts(snapshot_name, mounts_file="/proc/self/mounts"):
    """Every ``<dataset>/.zfs/snapshot/<snap>`` ZFS automount for this snapshot."""
    suffix = "/.zfs/snapshot/" + snapshot_name
    found = []
    try:
        with open(mounts_file, encoding="utf-8") as fh:
            for line in fh:
                parts = line.split()
                if len(parts) > 1:
                    mp = parts[1].replace("\\040", " ")
                    if mp.endswith(suffix):
                        found.append(mp)
    except OSError:
        return []
    return sorted(found, key=_depth, reverse=True)      # deepest first


def release_snapdirs(snapshot_name, runner=_run, mounts_file="/proc/self/mounts"):
    """Unmount ZFS's OWN snapshot automounts, so the snapshots can be destroyed.

    Reading anything under ``<dataset>/.zfs/snapshot/<snap>/`` makes ZFS **automount**
    that snapshot, and it stays mounted for ``zfs_expire_snapshot`` seconds (300 by
    default) after the last access. teardown() unmounts OUR bind mounts -- but the
    automount underneath them survives, and while it exists ``zfs destroy`` refuses
    with *"dataset is busy"*.

    Proven on a real pool: a 256-snapshot recursive tree swept cleanly except for the
    three datasets restic had read most recently. Those failed with EBUSY, and because
    cleanup_task removed the sidecar anyway, they were orphaned **permanently** -- a
    small leak, but a growing one, and exactly the failure this module exists to
    prevent.

    Deepest first, so a child's automount is released before its parent's.
    """
    errors = []
    for mp in snapdir_automounts(snapshot_name, mounts_file=mounts_file):
        res = runner(["umount", mp])
        if res.returncode != 0:
            errors.append(f"{mp}: {(res.stderr or '').strip()}")
    return errors


# ── orchestration (middleware is duck-typed; no middlewared import) ───────────
#
# These are SYNCHRONOUS and talk to middlewared via `middleware.call_sync`, which
# is safe from a worker thread and deadlocks on the event loop. That is the whole
# reason this file has one implementation instead of two:
#
#   TrueNAS <= 25.10  cloud_backup is async. The injected wrapper is `async def` and
#                     hands these to `await middleware.run_in_thread(...)`, which is
#                     exactly the thread `call_sync` needs.
#   TrueNAS >= 26     cloud_backup is synchronous and already runs in middlewared's
#                     thread pool (its own code calls `call_sync`). The injected
#                     wrapper calls these directly.
#
# So the async/sync difference lives entirely in the three injected blocks, and the
# logic below -- the part with the snapshots, the bind mounts and the failure modes
# -- is written once. Duplicating it as an async twin would mean every future fix
# had to be made twice, and the one that got missed would be the one that eats a
# backup.


def get_dataset_recursive(datasets, directory):
    """The dataset containing `directory`, and whether anything is nested under it.

    Vendored from middlewared's own plugins/cloud/snapshot.py (TrueNAS <= 25.10),
    because TrueNAS 26 DELETED it -- create_snapshot there uses filesystem.statfs
    instead. The injected block used to call it out of the host module's namespace,
    which on 26 is a straight NameError.

    Carrying our own copy removes the dependency on both versions rather than adding
    an assumption about it. It is ~10 lines of pure list arithmetic over data we
    already have in hand, and it has no reason to change.

    Returns (dataset, has_children):
      dataset      -- the DEEPEST dataset whose mountpoint is a prefix of `directory`
      has_children -- whether any OTHER dataset is mounted beneath `directory`
    """
    datasets = [
        dict(dataset, prefixlen=len(
            os.path.dirname(os.path.commonprefix(
                [dataset["properties"]["mountpoint"]["value"] + "/", directory + "/"]))
        ))
        for dataset in datasets
        if dataset["properties"]["mountpoint"]["value"] != "none"
    ]

    dataset = sorted(
        [
            dataset
            for dataset in datasets
            if (directory + "/").startswith(dataset["properties"]["mountpoint"]["value"] + "/")
        ],
        key=lambda dataset: dataset["prefixlen"],
        reverse=True,
    )[0]

    return dataset, any(
        (ds["properties"]["mountpoint"]["value"] + "/").startswith(directory + "/")
        for ds in datasets
        if ds != dataset
    )


def delete_snapshot_tree(middleware, snapshot, logger=None, attempts=4,
                         sleep=time.sleep, list_snapshots=None):
    """Delete the parent snapshot AND every child created by ``zfs snapshot -r``.

    Returns the snapshots it could NOT delete -- callers must not throw that away.

    ``zfs.snapshot.delete`` is non-recursive by default and stock calls it with
    no options, so relying on stock would orphan one snapshot per descendant
    dataset on every run. Idempotent: tolerates the parent already being gone
    (stock's ``finally`` may have won the race once our mounts were released).

    "dataset is busy" is EXPECTED here and is TRANSIENT. ZFS automounts
    ``<dataset>/.zfs/snapshot/<snap>`` when it is read and keeps it mounted for
    ``zfs_expire_snapshot`` seconds (300 by default) afterwards. So the datasets restic
    touched last are still pinned when we try to destroy them. We release the
    automounts explicitly and then retry -- on a real 256-snapshot tree, exactly three
    snapshots hit this, and before the fix they were orphaned permanently.
    """
    dataset, _, snapname = snapshot.partition("@")
    snaps = _Snapshots(middleware, list_snapshots)

    # Release ZFS's own automounts first, or `zfs destroy` refuses with EBUSY on
    # everything restic read in the last few minutes.
    for err in release_snapdirs(snapname):
        if logger:
            logger.debug("truecloud-patch: could not release snapdir %s", err)

    # Fast path: ONE recursive delete removes the parent and every child that
    # `zfs snapshot -r` created (252 on a real pool). Deleting them individually
    # also works, but it is neither cheap nor atomic -- a run killed part-way
    # through 252 sequential deletes leaves exactly the orphans this function
    # exists to prevent.
    try:
        snaps.delete(snapshot, recursive=True)
        return []
    except Exception as e:  # noqa: BLE001 - fall through to the explicit sweep
        # "Parent already gone" is the EXPECTED race (stock's finally won, once our
        # mounts were released) and happens on every clean run, so it is debug.
        # Anything else is a real fault -- a namespace that cannot delete, a schema
        # change, a permission error -- and this is the only place it is visible,
        # because the sweep below will report a different, downstream failure. At
        # debug it would never reach disk on stock middlewared, which logs at INFO.
        if logger:
            expected = "does not exist" in str(e).lower()
            (logger.debug if expected else logger.warning)(
                "truecloud-patch: recursive delete of %s failed (%r); sweeping "
                "the tree by name instead", snapshot, e,
            )

    # The parent may already be gone -- stock's `finally` can win the race once
    # our mounts are released -- which fails the recursive delete while the
    # children survive. Sweep them by name.
    try:
        # From ZFS, not middleware: the snapshot query hides internal datasets'
        # snapshots (205 of 274 on the test box), and a sweep that cannot see them
        # orphans one per hidden dataset on every run. See list_snapshot_names().
        #
        # An empty result means the tree is already gone -- delete nothing, and
        # do not fall back to the parent, which would only log a spurious
        # "does not exist" warning on every clean run.
        names = snapshot_tree_names(snapshot, snaps.names(dataset))
    except Exception as e:  # noqa: BLE001 - fall back to at least the parent
        if logger:
            logger.warning(
                "truecloud-patch: could not enumerate snapshot tree for %s: %r",
                snapshot, e,
            )
        names = [snapshot]

    def confirm_gone(failed):
        """Drop any name ZFS no longer has, even though its delete raised.

        A delete that raised "does not exist" SUCCEEDED as far as we care, and must
        not be retried or reported. The query is only a refinement: if it cannot be
        answered we keep the delete's own verdict, rather than inventing survivors --
        a false survivor keeps the sidecar forever and is reported as a leak that
        isn't there.
        """
        if not failed:
            return []
        try:
            live = set(snaps.names(dataset))
        except Exception:  # noqa: BLE001 - cannot refine; trust the delete's verdict
            return list(failed)
        return [n for n in failed if n in live]

    remaining = list(names)
    last_error = {}
    for attempt in range(attempts):
        failed = []
        for name in remaining:
            try:
                snaps.delete(name)
            except Exception as e:  # noqa: BLE001 - busy, or already gone; sorted below
                # KEEP the reason. This used to discard it and then report every
                # survivor as "(still busy?)" -- which names the one cause that is
                # benign and self-healing, and hides the ones that are permanent (a
                # namespace that cannot delete, a permission error, a schema change).
                # A misleading diagnosis is worse than none: it tells you to wait.
                last_error[name] = e
                failed.append(name)

        remaining = confirm_gone(failed)
        if not remaining:
            return []

        if attempt < attempts - 1:
            # EBUSY is the automount expiring. Release again (anything that walks
            # .zfs can re-automount a snapshot) and give it a moment.
            release_snapdirs(snapname)
            sleep(5)

    for name in remaining:
        if logger:
            logger.warning(
                "truecloud-patch: could not delete snapshot %s after %d attempts "
                "(last error: %r) -- it stays recorded and the next run reclaims it",
                name, attempts, last_error.get(name),
            )
    return remaining


def mounted_snapshots(mounts_file="/proc/self/mounts"):
    """Every ZFS snapshot something is currently mounted from.

    The device field of a snapshot mount IS the snapshot name (`Tap/apps/x@snap`), for
    both our staging bind mounts and ZFS's own .zfs automounts. So this is a direct,
    factual answer to "is anything using this snapshot right now" -- which is what
    protects a concurrently-running backup from the garbage collector, rather than
    trusting an age heuristic to be generous enough.
    """
    live = set()
    try:
        with open(mounts_file, encoding="utf-8") as fh:
            for line in fh:
                dev = line.split(" ", 1)[0]
                if "@" in dev:
                    live.add(dev.replace("\\040", " "))
    except OSError:
        return set()
    return live


def gc_stale_snapshots(middleware, task_name, current_snapshot, logger=None,
                       now=None, mounts_file="/proc/self/mounts", list_snapshots=None):
    """Delete snapshots this task left behind in an earlier run. Returns what remains.

    The backstop for when the RECORD is gone, not just the snapshots: the sidecar lives
    in /run (tmpfs), so a reboot mid-backup takes it with them. Without this, that tree
    -- one snapshot per descendant dataset, 250+ on a real pool -- is orphaned with
    nothing left pointing at it.

    Selection is `stale_snapshot_names()`, which is pure and heavily tested, because a
    name match is a weaker claim than a recorded fact and this deletes data on one.
    """
    dataset = current_snapshot.partition("@")[0]
    now = now or datetime.datetime.now(datetime.UTC)
    snaps = _Snapshots(middleware, list_snapshots)

    try:
        # From ZFS: middleware's snapshot query hides internal datasets, and an
        # orphan it cannot see is an orphan nothing will ever collect.
        all_names = snaps.names(dataset)
    except Exception as e:  # noqa: BLE001 - cannot enumerate; collect nothing
        if logger:
            logger.warning(
                "truecloud-patch: could not enumerate snapshots for GC: %r", e
            )
        return []

    stale = stale_snapshot_names(
        task_name, current_snapshot, all_names, now,
        in_use=mounted_snapshots(mounts_file),
    )
    if not stale:
        return []

    if logger:
        logger.warning(
            "truecloud-patch: %d snapshot(s) from an earlier run of %s were never "
            "cleaned up (a lost record, e.g. a reboot mid-backup); collecting them",
            len(stale), task_name,
        )

    remaining = []
    for name in stale:
        try:
            snaps.delete(name)
        except Exception as e:  # noqa: BLE001 - busy, or gone; either way, next run
            remaining.append(name)
            if logger:
                logger.debug(
                    "truecloud-patch: could not collect %s: %r", name, e
                )
    return remaining


def own_snapshot(middleware, task_name, snapshot, logger=None, list_snapshots=None):
    """Take ownership of `snapshot`'s whole tree: reclaim, collect, and record it.

    Call this on EVERY ``snapshot = true`` cloud_backup run — **whether or not the
    tree gets staged**. That unconditionality is the fix for a real leak, so do not
    make it conditional again.

    Stock decides whether to take a RECURSIVE snapshot by its own rule, and that
    rule is not ours:

    ``<= 25.10``
        stock's ``create_snapshot`` calls ``get_dataset_recursive()`` — the very
        function this module vendors. "Stock went recursive" and "we have something
        to stage" were therefore the *same question*, and a non-staged snapshot
        provably had no children. Stock's non-recursive delete was correct.

    ``26``
        stock uses ``filesystem.statfs``: ``recursive = (path == the dataset's
        mountpoint)``. Now the two rules disagree. A dataset whose only descendants
        are **ZVOLs** or **legacy/none-mountpoint** datasets gets a RECURSIVE
        snapshot — while ``get_dataset_recursive()`` reports nothing to stage,
        because neither kind is a mounted filesystem under ``path``.

    In that gap stock takes one snapshot per descendant and then deletes only the
    parent (its ``finally`` destroys ``path=snapshot``, non-recursively). Nothing
    would ever have found the children: no staging tree, so no sidecar, and the GC
    only ever ran from :func:`stage_nested`. One orphan per zvol/legacy descendant,
    on every run, forever — while the backup reports SUCCESS. That is the exact
    failure this module exists to prevent, reintroduced by a gate.

    So ownership of the sweep is no longer conditional on staging. It is cheap:
    :func:`delete_snapshot_tree` is idempotent, and on a genuinely childless
    snapshot it is one recursive destroy of a snapshot stock has usually already
    removed.

    Returns the staging root; the sidecar sits beside it.
    """
    staging_root = staging_root_for(task_name)

    # A previous run may have crashed mid-flight; never build on top of that.
    teardown(staging_root)

    # ...and if it left snapshot trees behind, they are still on disk and nothing else
    # will ever reclaim them. Sweep them before recording our own, or a single crashed
    # run orphans 160+ snapshots permanently.
    #
    # Anything a reclaim FAILS to delete is carried forward, not dropped. Overwriting
    # the sidecar with only our own snapshot is what destroyed the record of a survivor
    # once already: the reclaim ran, hit ZFS's 300-second automount window (the runs
    # were minutes apart), left one snapshot behind, and then the record of it was
    # overwritten -- a permanent orphan, created by the very code meant to prevent one.
    pending = []
    for stale in _read_sidecar(staging_root):
        if stale == snapshot:
            continue
        if logger:
            logger.warning(
                "truecloud-patch: reclaiming snapshot tree from an earlier "
                "run: %s", stale,
            )
        pending.extend(delete_snapshot_tree(
            middleware, stale, logger=logger, list_snapshots=list_snapshots))

    if pending and logger:
        logger.warning(
            "truecloud-patch: %d snapshot(s) from an earlier run are still busy; "
            "carrying them forward to the next run", len(pending),
        )

    # ...and collect anything from an earlier run that has NO record at all.
    #
    # The sidecar above is precise but lives in /run, which is tmpfs -- a reboot
    # mid-backup destroys it and orphans the whole tree with nothing pointing at it.
    # This finds those by name and is the only thing that ever will.
    #
    # It runs AFTER the sidecar reclaim on purpose: the recorded path is authoritative
    # and cheap, and the GC should only ever be mopping up what the record lost.
    pending.extend(gc_stale_snapshots(
        middleware, task_name, snapshot, logger=logger,
        list_snapshots=list_snapshots,
    ))

    # Record the snapshot BEFORE mounting anything, not after. middlewared can
    # die at any point (this patch even schedules a restart at boot), and the
    # sidecar is the only thing that survives it -- an in-process dict would take
    # the sole record of a 160-snapshot tree with it. Writing it after apply_plan
    # would leave exactly the crash window the sidecar exists to close.
    _write_sidecar(staging_root, [*pending, snapshot])
    return staging_root


def stage_nested(middleware, path, snapshot, base_dataset, base_mountpoint,
                 task_name, datasets, logger=None, list_snapshots=None):
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
    staging_root = own_snapshot(
        middleware, task_name, snapshot, logger=logger,
        list_snapshots=list_snapshots,
    )

    try:
        mounts, skipped = plan_staging(
            base_dataset, base_mountpoint, path, snapshot_name,
            datasets, staging_root,
        )
        if logger:
            for name, reason in skipped:
                logger.warning(
                    "truecloud-patch: not staging dataset %r: %s", name, reason
                )

        apply_plan(mounts)
        verify_staged(mounts)
    except Exception:
        # Tear down the mounts, but KEEP the sidecar.
        #
        # The caller (SNAPSHOT_BLOCK) sweeps the snapshot tree on the way out, and if
        # any of it is still busy it will survive -- and the sidecar is the only record
        # that it exists. Removing it here would orphan those snapshots permanently.
        #
        # The asymmetry is deliberate: a sidecar left behind when the tree is already
        # gone is harmless (the next run tries to delete a tree that is not there,
        # finds nothing, and moves on), while a sidecar removed while the tree still
        # exists is unrecoverable. Only a confirmed-clean sweep removes it -- see
        # cleanup_task().
        teardown(staging_root)
        raise

    if logger:
        logger.info(
            "truecloud-patch: staged %d dataset(s) from %s at %s",
            len(mounts), snapshot, staging_root,
        )
    return staging_root


def cleanup_task(middleware, task_name, logger=None, list_snapshots=None):
    """Tear down a task's staging tree and delete the snapshot it pinned.

    Safe to call unconditionally: a no-op when the task was never staged.
    """
    staging_root = staging_root_for(task_name)
    pinned = _read_sidecar(staging_root)

    if not pinned and not os.path.isdir(staging_root):
        return  # never staged; nothing to do

    errors = teardown(staging_root)
    if errors and logger:
        for err in errors:
            logger.warning("truecloud-patch: staging teardown: %s", err)

    if not pinned:
        _remove_sidecar(staging_root)
        return

    # Every tree this task still owns -- ours, plus anything an earlier run could not
    # finish reclaiming.
    survivors = []
    for snapshot in pinned:
        survivors.extend(delete_snapshot_tree(
            middleware, snapshot, logger=logger, list_snapshots=list_snapshots))

    # KEEP the sidecar if anything survived. It is the only record that those
    # snapshots exist, and removing it orphans them permanently.
    #
    # That is not theoretical: on a real 256-snapshot tree, three snapshots were still
    # pinned by ZFS's own .zfs/snapshot automount (which lingers for 300s after the
    # last read), failed to delete with "dataset is busy", and the sidecar was removed
    # anyway -- so nothing would ever have reclaimed them. A small leak, but one that
    # grows by a few snapshots on every single run, forever.
    #
    # Left in place, the next run's stage_nested() sees a stale sidecar naming a
    # different snapshot and sweeps that tree first -- by which time the automounts are
    # long gone and the delete succeeds.
    if survivors:
        if logger:
            logger.warning(
                "truecloud-patch: %d snapshot(s) could not be deleted (still busy); "
                "recording them so the next run reclaims them: %s",
                len(survivors), ", ".join(survivors),
            )
        # The SURVIVORS, not the trees we asked to delete. Writing the original list
        # back would keep re-sweeping trees that are already gone.
        _write_sidecar(staging_root, survivors)
        return

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
        for snap in read_sidecar(sc[: -len(".snapshot")]):
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
