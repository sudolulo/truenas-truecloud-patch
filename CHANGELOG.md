# Changelog

## v0.3.0 — 2026-07-12

### Added

- **`snapshot = true` now works on datasets that have child datasets** —
  **opt-in, off by default** (`install.sh --enable-nested-snapshots` /
  `--disable-nested-snapshots`). It changes how backups read their source data,
  so it is never enabled implicitly; with neither flag `install.sh` preserves
  the existing setting, so a `git pull && bash install.sh` cannot silently flip
  it. When disabled, `apply.sh` skips the patch entirely and the stock guard
  remains. `uninstall.sh` tears down any staging mounts and removes the marker.
  Stock TrueNAS refuses this with *"This option is only available for datasets
  that have no further nesting"*, which makes the snapshot option unusable for
  the single most common case on any box running Apps — every app is its own
  dataset, often with `config`/`pgdata` children of its own. Without it, the
  backup reads **live** files: databases are captured mid-write, and a busy app
  rewriting its files can stall a backup indefinitely as restic chases a moving
  target.

  The stock guard is **correct, and it is not an arbitrary limit.**
  `plugins/cloud/snapshot.py` already takes a *recursive* ZFS snapshot, but it
  then points the backup tool at the **parent** dataset's
  `.zfs/snapshot/<snap>/` directory — and ZFS does not expose child datasets
  through a parent's snapshot directory:

  ```
  /mnt/Tap/.zfs/snapshot/<snap>/apps/               -> 0 entries (children invisible)
  /mnt/Tap/apps/lidarr/config/.zfs/snapshot/<snap>/ -> the real data
  ```

  So without the guard the backup tool would walk a near-empty tree, report
  SUCCESS, and upload almost nothing. iX gate the config rather than ship a
  backup that lies about succeeding.

  This release implements the missing half. After the (already recursive)
  snapshot is taken, every descendant dataset's own `.zfs/snapshot/<snap>` is
  bind-mounted into a **staging tree** mirroring the original layout, and the
  backup tool is pointed at the staging root — a complete, consistent,
  point-in-time view of the whole subtree. Only then is the guard relaxed.

  Safety properties, in order of importance:

  - **Staging failure is loud.** If any descendant cannot be staged, the backup
    fails. A silently-incomplete backup is the exact outcome the stock guard
    exists to prevent, and it would be worse than not having the feature.
  - **A post-mount verification pass** asserts every planned target is really a
    mountpoint and the staging root is non-empty, so this can never regress into
    the empty-backup failure it is meant to fix.
  - **The guard is relaxed last.** `apply.sh` installs the traversal, patches
    `snapshot.py`, then `sync.py`, and only then `crud.py`. A partial failure
    leaves the guard intact and the option merely unavailable — never
    "guard removed, traversal missing".
  - **The patch owns the whole snapshot lifecycle.** `zfs.snapshot.delete`
    defaults to `recursive=False` and stock `restic_backup()` calls it with no
    options. Stock gets away with that only because its validation means
    `recursive` is never True in the field — but enabling nested datasets makes
    recursive snapshots real, so the parent now has one child snapshot per
    descendant dataset (160+ on a typical Apps pool). Relying on stock's delete
    would therefore orphan every child snapshot **on every successful run**.
    This patch sweeps the parent *and* all children, is idempotent against
    stock's `finally` winning the race, records the snapshot in a sidecar file
    (so a middlewared restart mid-backup cannot orphan it), reclaims the tree
    left by a crashed run, and deletes the tree when staging fails — where
    sync.py's own `finally` would otherwise delete nothing at all, because its
    `snapshot` local never gets assigned.
  - **The dataset list is enumerated *after* the snapshot, never before.** A
    list read beforehand can miss a dataset created in the gap: the recursive
    snapshot would capture it but the staging plan would not, silently omitting
    its data. Read afterwards, an unsnapshotted dataset trips the staging check
    and fails the run loudly instead.
  - **Every injected block no-ops** if `_truecloud_nested` is absent.
  - Datasets that cannot contribute to a file tree (`mountpoint=none|legacy`,
    unmounted/locked, encrypted-and-locked) are skipped and **reported** —
    never dropped silently.
  - Scoped to `cloud_backup` only. Cloud Sync (rclone) shares the same
    validation mixin but has no staging teardown wired in, so its guard is left
    in place deliberately.

  Side benefit: the staging root is a **stable** path per task, so restic can
  find its parent snapshot between runs. Stock's
  `.zfs/snapshot/<name>-<timestamp>/` path changes every run, which defeats
  restic's parent detection and forces a full re-scan each time.

- **CI** (GitHub Actions): shellcheck + `bash -n` on every script, ruff, and
  pytest on Python 3.11/3.12/3.13. Includes tests that `compile()` the
  `*_BLOCK` strings — they are Python source appended to live middlewared
  modules, so a syntax error there would break the box at boot, and nothing
  previously checked them.

### Changed

- **The patch is now two independent modules, and each retires on its own.**
  Previously the native-support check looked only for native B2 restic support
  and, on finding it, set the kill switch and disabled *everything*. With a
  second capability in the patch that would silently take a still-needed module
  down with the superseded one — TrueNAS is likely to ship one of these long
  before the other.

  `apply.sh` now detects each separately (`providers`: does `B2RcloneRemote`
  carry a real `get_restic_config()`; `nested`: is the *"no further nesting"*
  validation still in `plugins/cloud/crud.py`), skips just the superseded one,
  and only sets the kill switch once **both** are done. The UI patch belongs to
  `providers` and is skipped with it. The deferred middlewared restart now fires
  when *any* still-needed module landed — keying it off `providers` alone would
  have left a freshly-patched `nested` module on disk and never loaded on a
  native-B2 box. `hook_status.json` reports each module with an `active` flag and
  a reason.

- README rewritten to be less alarmist: dropped the warning boxes and the
  disclaimer's fear-bulleting in favour of plain statements, and documented the
  two-module design. The one caveat kept as a plain sentence: the `mount --bind`
  staging step has not yet been exercised by a live backup run.

- Version strings in `install.sh`, `uninstall.sh`, and `recover.sh` were stale
  at `0.0.4`; all scripts now report the same version.
- `patch_ui.py`: replaced a `try`/`except`/`pass` with `contextlib.suppress`
  (no behaviour change; satisfies the new lint gate).

### Removed

- `patch/__pycache__/create_task.cpython-314.pyc` was committed to the
  repository; it is now untracked and `__pycache__/` is gitignored.

### Fixed (post-merge audit)

- **`create_task.py verify` failed on a default install.** `hook_status.json`
  emitted a per-file entry for the nested module with `ok: false` whenever the
  feature was switched off — which is the default — so `verify` printed `[FAIL]`
  and exited 1 right after the README told users to run it. Status is now
  reported per *module* with an `active` flag, and `verify` renders an inactive
  module as `[SKIP]` rather than a failure.
- **A partial apply suppressed the middlewared restart.** The exit code
  conflated "nothing applied" with "one module applied, one failed", so a failing
  providers patch would prevent the restart that a freshly-applied nested patch
  needs — leaving it on disk and never loaded. Exit 2 now means partial, and the
  restart still fires.
- **The native-nested probe could never fire.** It scanned `crud.py` for the
  guard message, but our own injected block *quotes* that message, so once
  applied the probe would always conclude the guard was still present. It now
  reads only the stock portion of the file.
- `recover.sh` did not unmount staging trees, so an emergency recovery left bind
  mounts pinning ZFS snapshots that could then never be destroyed.
- `uninstall.sh` deleted sidecar files without reading them. A sidecar is the
  only record that an interrupted run's snapshot tree is still on disk; both
  scripts now name the snapshot (`zfs destroy -r ...`) before clearing it.

### Refactored

- Staging teardown had been copy-pasted into `uninstall.sh` and `recover.sh` —
  two untested shell copies of the fiddly depth-ordering and lazy-umount logic.
  Both now call `python3 patch/truecloud_nested.py cleanup`, so there is one
  implementation and it is the one under test.
- Dropped the in-memory `ACTIVE` dict. The sidecar file was already the source of
  truth; a second in-process record could only desync — and it is the
  middlewared-restart case (which empties it) that must not orphan a snapshot
  tree. One record, on disk, or none.

### Known issues

- Stock `restic_backup()` deletes the ZFS snapshot in its own `finally`, which
  fails with `EBUSY` while the staging bind mounts pin it. It logs one benign
  `Error deleting snapshot ...` warning per run; the patch then unmounts and
  deletes the snapshot for real. The warning is expected and harmless.

## v0.2.1 — 2026-07-09

### Fixed

- **Deferred restart raced the rest of boot, leaving all apps and dashboard
  stats down.** The `truecloud-mw-restart` unit introduced in v0.0.4 relied
  on systemd ordering (`After=multi-user.target`, `After=ix-postinit.service`),
  which cannot see middlewared's *internal* boot work. Observed on 25.10.4:
  the restart fired two seconds into `ix-reporting.service`'s
  `midclt call reporting.start_service` and before the docker/apps startup
  task (created on middlewared's system-ready event) had run. Both were
  killed, and nothing retries them until the next boot — every app stayed
  down (`docker.status` FAILED, the apps dataset never mounted), netdata
  never started (no dashboard hardware stats), and the SMB middleware
  backend was left uninitialized.

  The transient unit now runs `patch/wait_restart.sh` instead of restarting
  directly: it waits for the systemd boot job queue to drain
  (`systemctl is-system-running --wait`, covering in-flight `ix-*` oneshots
  such as ix-reporting), then polls `midclt call docker.status` until the
  docker state machine leaves its transitional states, then allows a short
  grace period for middleware-internal tasks with no queryable state before
  issuing `systemctl try-restart middlewared`. The unit no longer sets
  `Type=oneshot` — a oneshot's start job stays in the very queue the script
  waits on and would deadlock on itself. All waits are bounded and fail
  open: worst case the restart still happens, just later.

  Recovery on a boot that already hit this (without rebooting):
  `midclt call reporting.start_service` and
  `midclt call docker.state.start_service true`.

## v0.2.0 — 2026-07-08

### Changed

- **`create_task.py` now uses the TrueNAS middleware via `midclt` instead of the
  deprecated `/api/v2.0` REST API**, which is removed in TrueNAS 26.04. Practical
  effects:
  - Run the script **on the TrueNAS host** — it uses the local middleware socket, so
    it no longer needs a host address or API key.
  - `--host`, `--api-key`, and `--insecure` are accepted but **ignored** (a deprecation
    note is printed); they will be removed in a future release.
  - `list-credentials` → `cloudsync.credentials.query`, `list-tasks` →
    `cloud_backup.query`, `create` → `cloud_backup.create`.
- Dropped the `ssl`/`urllib` HTTP client; no TLS certificate handling is needed anymore.

## v0.1.0 — 2026-07-08

### Added

- `create --cache-path PATH` — sets the restic cache directory on the task.
  Without a cache path, TrueNAS runs restic with `--no-cache`, which re-reads all
  repository metadata from the provider on every run and is glacially slow on
  large repos (a 564 GB dataset estimated **55 days** to a first backup). Tasks
  created without `--cache-path` now print a warning explaining the consequence.

## v0.0.4 — 2026-07-06

### Fixed

- **Backend patch inactive after every reboot.** PREINIT initshutdownscripts
  are executed by middlewared itself (`ix-preinit.service` runs
  `midclt call initshutdownscript.execute_init_tasks PREINIT`, ordered after
  `ix-zfs.service` pool import). By the time `apply.sh` patched `b2.py` and
  `restic.py` in the overlay, the running middlewared had already imported the
  stock modules and never re-imports — so S3/B2 support silently reverted on
  every reboot until something restarted middlewared. `install.sh` masked the
  bug because it restarts middlewared explicitly.

  Fix: when `apply.sh` detects it was invoked by middlewared (boot context),
  it now schedules a single detached restart via a transient systemd unit
  (`truecloud-mw-restart`, ordered after `multi-user.target` and
  `ix-postinit.service`) so the patched modules are loaded once boot settles.
  The restart is never synchronous — `apply.sh` is a child of middlewared's
  own job runner, and later `ix-*` boot units still need midclt. Manual runs
  of `apply.sh` never trigger a restart.

- **`TypeError: string indices must be integers` when creating a B2 task on
  TrueNAS 24.10 (Electric Eel)** (#1). The credential schema differs between
  releases: on 24.10 `credentials["provider"]` is the type string (`"B2"`)
  with the account/key in `credentials["attributes"]`, while 25.04+ moved
  them into a provider dict. The injected `get_restic_config` only handled
  the 25.04+ shape. It now detects the schema and reads the credentials from
  the right place on both; `create_task.py list-credentials` and `list-tasks`
  got the same treatment.

- **`create_task.py verify` false-positive after reboot.** `verify` trusted
  `hook_status.json`, which only records that the files were patched on disk —
  not that the running process loaded them. `verify` now also compares the
  middlewared main-process start time against `patched_at` and reports FAIL
  (with recovery instructions) when the process predates the patch.

### Changed

- README and script comments no longer claim PREINIT runs "before middlewared
  starts"; the boot ordering and the deferred restart are now documented.
- `recover.sh` and `uninstall.sh` cancel a still-queued deferred restart
  before performing their own, and their re-enable instructions now include
  the required `systemctl restart middlewared`.

---

## v0.0.3 — 2026-06-22

### Fixed

- **`patch/apply.sh` silently killed by the 10-second PREINIT timeout.**
  TrueNAS PREINIT initshutdownscripts have a 10-second default timeout. The
  previous apply.sh ran approximately 8 Python subprocesses (each ~1-2 s), so it
  was routinely killed mid-run. Symptoms: patches not applied after reboot, but
  re-running `bash apply.sh` manually (no timeout) always succeeded.

  Fix: `install.sh` now registers the hook with `"timeout": 120`. Existing
  installations are updated to the new timeout on the next `bash install.sh` run.

  Additionally, `patch/apply.sh` consolidates its Python subprocess invocations
  from ~8 down to 2, reducing startup overhead from ~12-16 s to ~2-4 s — well
  within the new 120-second budget.

### Changed

- `find_mw_python` in `apply.sh` no longer spawns a separate Python process to
  verify the interpreter can import `middlewared`. Verification is now implicit in
  the combined path-discovery subprocess that follows.

---

## v0.0.2 — 2026-06-19

### Fixed

- **B2 backup failing with `NotImplementedError` after a TrueNAS update.**
  The patch block's guard (`if "get_restic_config" not in B2RcloneRemote.__dict__`)
  could misfire and silently skip injecting the method — most likely when a
  TrueNAS version adds a stub that raises `NotImplementedError`, causing the dict
  check to return `False`. The guard is removed; the assignment is now
  unconditional. This is safe because the native-support kill switch already
  prevents patching when TrueNAS ships a real, working implementation.

- **Native-support check falsely triggering kill switch on stubs.**
  The check now inspects the source of any pre-existing `get_restic_config`
  before concluding that TrueNAS has shipped native B2 support. If the method
  body contains `NotImplementedError` it is treated as a stub and patching
  continues; only a method that does not raise `NotImplementedError` triggers
  the kill switch and auto-disable.

---

## v0.0.1 — 2026-06-16

Initial public release. Extends TrueNAS SCALE's TrueCloud Backup feature to
work with S3-compatible providers and native Backblaze B2 in addition to Storj,
using volatile overlayfs patching of the TrueNAS middleware that persists across
system updates via a PREINIT initshutdownscript.

### Included

- `install.sh` — registers the PREINIT boot hook and applies patches immediately
- `patch/apply.sh` — PREINIT script; mounts writable overlays, patches `b2.py`
  and `restic.py`, patches the Angular UI bundle to widen the credential dropdown
- `patch/create_task.py` — CLI to create TrueCloud Backup tasks with S3 or B2
  credentials, bypassing the Storj-only restriction in the UI
- `recover.sh` — emergency recovery; sets the kill switch and restarts middlewared
- `uninstall.sh` — full removal of the patch and PREINIT hook
- Kill switch support (`disabled` file) for safe degradation
- Auto-disable when TrueNAS ships native B2 restic support
- `hook_status.json` written on each boot for `create_task.py verify`
