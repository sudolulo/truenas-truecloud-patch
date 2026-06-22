# Changelog

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
