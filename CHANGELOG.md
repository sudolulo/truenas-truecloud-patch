# Changelog

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
