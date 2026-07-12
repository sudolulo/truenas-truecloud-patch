#!/bin/bash
# patch/apply.sh — registered as a TrueNAS PREINIT initshutdownscript.
#
# PREINIT scripts are executed BY middlewared itself (ix-preinit.service runs
# `midclt call initshutdownscript.execute_init_tasks PREINIT`, ordered after
# ix-zfs.service pool import). So when this script runs at boot, middlewared
# is already up and has already imported the stock modules — the on-disk
# patch alone cannot reach the running process.
#
# TrueNAS updates replace /usr/ entirely; this script re-applies three patches:
#
#   1. Backend — b2.py and restic.py are patched directly in the overlay.
#      On a boot run, a single detached middlewared restart is scheduled
#      (Step 3) so the patched modules actually get loaded.
#
#   2. Nested-dataset snapshots — installs _truecloud_nested.py and patches
#      plugins/cloud/{snapshot,crud}.py + plugins/cloud_backup/sync.py so the
#      "Take Snapshot" option works on a dataset that has child datasets.
#      Stock middleware refuses that config, because it points the backup tool
#      at the PARENT's .zfs/snapshot/ where children are invisible — it would
#      silently back up a near-empty tree. We stage a complete tree of
#      per-dataset bind mounts and only then relax the guard.
#
#   3. Angular JS bundle — Widens the TrueCloud Backup credential dropdown
#                          from Storj-only to include S3 and B2. Served from
#                          disk per request, so no restart is needed for it.
#
# Design principle: every step is independently fail-safe.
# A failed patch logs a warning and continues; middlewared always starts.
# Never use `set -e` in a PREINIT script.

# Derive PATCH_DIR from this script's location (parent of the patch/ directory).
PATCH_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$PATCH_DIR/apply.log"
VERSION="0.3.0"

# Rotate log at 512 KB to avoid unbounded growth on a system volume.
# Keep two prior generations (.1 and .2) so the last three boots are always available.
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 524288 ]; then
    [ -f "${LOG}.1" ] && mv "${LOG}.1" "${LOG}.2"
    mv "$LOG" "${LOG}.1"
fi

exec >> "$LOG" 2>&1
echo "=== $(date -Iseconds) [v${VERSION}] ==="

# Kill switch: if this file exists, skip all patching and exit cleanly.
# Recovery: touch "$PATCH_DIR/disabled" (then reboot or restart middlewared).
if [ -f "$PATCH_DIR/disabled" ]; then
    echo "Kill switch active ($PATCH_DIR/disabled exists) — patch not applied."
    echo "To re-enable: rm $PATCH_DIR/disabled"
    echo "=== done ==="
    exit 0
fi

# Mounts a writable overlayfs on $1 using /run (tmpfs) for the upper/work dirs
# when the directory is read-only.  The overlay is volatile per boot; this
# PREINIT script recreates it on every boot.
# Returns 0 if the directory is now writable, 1 if it could not be made so.
_ensure_writable() {
    local dir="$1" tag="$2"
    # Already writable?
    if touch "$dir/.truecloud-probe" 2>/dev/null; then
        rm -f "$dir/.truecloud-probe"
        return 0
    fi
    # Already our overlay on this exact directory from an earlier run this boot?
    if mount | grep -qF "truecloud-${tag} on ${dir} "; then
        return 0
    fi
    local upper="/run/truecloud-${tag}-upper" work="/run/truecloud-${tag}-work"
    mkdir -p "$upper" "$work"
    if mount -t overlay "truecloud-${tag}" \
           -o "lowerdir=$dir,upperdir=$upper,workdir=$work" "$dir" 2>/dev/null; then
        echo "OK: Mounted writable overlay on $dir"
        return 0
    fi
    echo "WARNING: overlay mount failed on $dir — backend patch will be skipped."
    return 1
}

# Find the Python interpreter that middlewared actually uses.
# On TrueNAS SCALE, /usr/bin/middlewared is usually a Python entry-point script
# with a shebang pointing at the right interpreter (system or venv).
# The shebang is read with dd (no Python startup cost); import verification is
# deferred to the combined subprocess below which handles failure gracefully.
find_mw_python() {
    local py="python3"
    local shebang=""

    if [ -x /usr/bin/middlewared ]; then
        # Read the first line safely (max 256 bytes) — avoids reading a binary ELF
        shebang=$(dd if=/usr/bin/middlewared bs=256 count=1 2>/dev/null | head -1 || true)

        if [[ "$shebang" =~ ^'#!'(/[^[:space:]]+python[^[:space:]]*) ]]; then
            py="${BASH_REMATCH[1]}"
        elif [[ "$shebang" =~ ^'#!/usr/bin/env '(python[^[:space:]]*) ]]; then
            py=$(command -v "${BASH_REMATCH[1]}" 2>/dev/null || echo "python3")
        fi
    fi

    echo "$py"
}

PYTHON=$(find_mw_python)
echo "Using Python: $PYTHON"

# ── Discover paths + native support check (single Python subprocess) ──────────
# Combines what were previously four separate Python invocations into one to
# avoid repeated interpreter startup overhead under the PREINIT timeout budget.

_tc_info=$("$PYTHON" -c "
import inspect, os, sys

result = {'native': 'no', 'site_pkg': '', 'mw_dir': ''}

try:
    import middlewared
    mw_file = os.path.abspath(middlewared.__file__)
    result['mw_dir']   = os.path.dirname(mw_file)
    result['site_pkg'] = os.path.dirname(os.path.dirname(mw_file))
except ImportError:
    try:
        import site
        result['site_pkg'] = site.getsitepackages()[0]
    except Exception:
        pass

try:
    import middlewared.rclone.remote.b2 as _b2_mod
    from middlewared.rclone.remote.b2 import B2RcloneRemote
    if 'get_restic_config' in B2RcloneRemote.__dict__:
        src = open(inspect.getfile(_b2_mod), encoding='utf-8', errors='replace').read()
        if 'TRUECLOUD_PATCH' not in src:
            try:
                method_src = inspect.getsource(B2RcloneRemote.get_restic_config)
            except (OSError, TypeError):
                method_src = ''
            if 'NotImplementedError' not in method_src:
                result['native'] = 'yes'
except Exception:
    pass

print(result['native'])
print(result['site_pkg'])
print(result['mw_dir'])
" 2>/dev/null || printf 'no\n\n\n')

_tc_native=$(printf '%s' "$_tc_info" | sed -n '1p')
SITE_PKG=$(printf '%s' "$_tc_info"  | sed -n '2p')
_MW_DIR=$(printf '%s' "$_tc_info"   | sed -n '3p')

if [ "$_tc_native" = "yes" ]; then
    echo "NOTICE: TrueNAS now provides native B2 restic support — truecloud-patch is no longer needed."
    echo "NOTICE: Setting kill switch; patching will be skipped on all future boots."
    echo "NOTICE: Run the following to fully remove the patch:"
    echo "NOTICE:   bash $PATCH_DIR/uninstall.sh"
    touch "$PATCH_DIR/disabled"
    for _tag in mw ui; do
        if mount | grep -qF "truecloud-${_tag} on "; then
            _mnt=$(mount | grep "truecloud-${_tag} on " | awk '{print $3}' | head -1)
            umount "$_mnt" 2>/dev/null && echo "NOTICE: Unmounted overlay on $_mnt" || true
        fi
    done
    echo "=== done ==="
    exit 0
fi

# ── Step 1: backend patch ─────────────────────────────────────────────────────

echo "--- backend patch ---"

_b2_ok=0
_restic_ok=0

if [ -z "$SITE_PKG" ]; then
    echo "WARNING: Cannot determine site-packages directory; skipping backend patch."
    echo "  Run: $PYTHON -c \"import site; print(site.getsitepackages())\""
elif ! _ensure_writable "$SITE_PKG" "mw"; then
    echo "WARNING: Cannot make site-packages writable; skipping backend patch."
elif [ -z "$_MW_DIR" ]; then
    echo "WARNING: Cannot determine middlewared directory; skipping backend patch."
else
    _B2_PY="$_MW_DIR/rclone/remote/b2.py"
    _RESTIC_PY="$_MW_DIR/plugins/cloud_backup/restic.py"
    _CLOUD_DIR="$_MW_DIR/plugins/cloud"
    _SYNC_PY="$_MW_DIR/plugins/cloud_backup/sync.py"
    _NESTED_SRC="$PATCH_DIR/patch/truecloud_nested.py"

    # Nested-dataset snapshot support is OPT-IN. It changes how backups read
    # their source data, so it is never enabled implicitly by a `git pull`.
    # Enable:  bash install.sh --enable-nested-snapshots
    # Disable: bash install.sh --disable-nested-snapshots
    if [ -f "$PATCH_DIR/nested_snapshots_enabled" ]; then
        _NESTED_ENABLED=1
    else
        _NESTED_ENABLED=0
    fi

        # ── patch b2.py + restic.py + nested-snapshot + hook_status.json ────────
        # (single subprocess: PREINIT has a tight timeout budget)
        if "$PYTHON" - "$_B2_PY" "$_RESTIC_PY" "$PATCH_DIR/hook_status.json" \
                      "$_CLOUD_DIR" "$_SYNC_PY" "$_NESTED_SRC" "$_NESTED_ENABLED" << 'PYEOF'
import json, os, shutil, sys, time

b2_path, restic_path, status_path = sys.argv[1], sys.argv[2], sys.argv[3]
cloud_dir, sync_path, nested_src = sys.argv[4], sys.argv[5], sys.argv[6]
nested_enabled = sys.argv[7] == "1"

B2_BLOCK = """
# TRUECLOUD_PATCH — added by truenas-truecloud-patch/patch/apply.sh
def _tc_get_restic_config(task):
    p = task["credentials"]["provider"]
    if not isinstance(p, dict):
        # TrueNAS <= 24.10: provider is the type string ("B2") and the
        # account/key live in the credential's attributes dict. 25.04+
        # moved them into a provider dict.
        p = task["credentials"]["attributes"]
    return "", {"B2_ACCOUNT_ID": p["account"], "B2_ACCOUNT_KEY": p["key"]}

B2RcloneRemote.get_restic_config = staticmethod(_tc_get_restic_config)
B2RcloneRemote.restic = True
"""

RESTIC_BLOCK = """
# TRUECLOUD_PATCH — added by truenas-truecloud-patch/patch/apply.sh
try:
    _tc_orig_get_restic_config = get_restic_config
except NameError:
    pass  # get_restic_config not in this module; TrueNAS restructured restic.py
else:
    def get_restic_config(cloud_backup):
        import dataclasses as _dc
        result = _tc_orig_get_restic_config(cloud_backup)
        cmd = list(result.cmd)
        for i, part in enumerate(cmd):
            if part.startswith("--repo=") or part.startswith("--repository="):
                pfx, _, url = part.partition("=")
                pfx += "="
            elif i and cmd[i - 1] in ("-r", "--repo", "--repository"):
                pfx = None
                url = part
            else:
                continue
            scheme, sep, rest = url.partition(":")
            if not sep:
                break
            changed = False
            if rest.startswith("/") and not rest.startswith("//"):
                rest = rest[1:]
                changed = True
            if scheme == "b2" and "/" in rest:
                rest = rest.replace("/", ":", 1)
                changed = True
            if changed:
                new_url = scheme + ":" + rest
                cmd[i] = pfx + new_url if pfx is not None else new_url
                try:
                    return _dc.replace(result, cmd=cmd)
                except TypeError:
                    return result._replace(cmd=cmd)
            break
        return result

    get_restic_config._truecloud_patched = True
"""

# ── nested-dataset snapshot support ───────────────────────────────────────────
# Stock middleware refuses `snapshot=true` on a path containing child datasets,
# because it points the backup tool at the PARENT dataset's .zfs/snapshot/,
# where child datasets are INVISIBLE -- it would silently back up a near-empty
# tree. That guard is correct. We implement the missing traversal (a staging
# tree of per-dataset bind mounts) and only then relax the guard.
#
# Fail-safe direction: if any of these three blocks fails to apply, the stock
# guard remains and the option simply stays unavailable. We never end up with
# the guard removed but the traversal missing -- that would be a silently empty
# backup, the worst possible outcome.

SNAPSHOT_BLOCK = """
# TRUECLOUD_PATCH — added by truenas-truecloud-patch/patch/apply.sh
try:
    from middlewared.plugins.cloud import _truecloud_nested as _tc_nested
except ImportError:
    _tc_nested = None

if _tc_nested is not None:
    _tc_orig_create_snapshot = create_snapshot

    async def create_snapshot(middleware, path, name="cloud_task-onetime"):
        # Stock takes the (already recursive) snapshot; we only replace the PATH.
        snapshot, snap_path = await _tc_orig_create_snapshot(middleware, path, name)

        _logger = getattr(middleware, "logger", None)
        try:
            # Enumerate datasets AFTER the snapshot, never before. The snapshot is
            # the point-in-time truth; a list read beforehand could miss a dataset
            # created in the gap, which the recursive snapshot WOULD capture but
            # our staging plan would not -- silently omitting it from the backup.
            # Read afterwards, an unsnapshotted dataset instead trips the isdir()
            # check in plan_staging and fails the run loudly. Loud beats silent.
            datasets = await middleware.call(
                "zfs.dataset.query", [["type", "=", "FILESYSTEM"]]
            )
            dataset, nested = get_dataset_recursive(datasets, path)

            if not nested:
                # No children: stock behaviour, untouched. Stock's `finally` owns
                # the snapshot from here (its non-recursive delete is correct,
                # because a non-nested snapshot has no children).
                return snapshot, snap_path

            staging_root = await _tc_nested.stage_nested(
                middleware, path, snapshot,
                dataset["name"], dataset["properties"]["mountpoint"]["value"],
                name, datasets, logger=_logger,
            )
        except Exception:
            # The snapshot exists, but this exception means sync.py never completes
            # `snapshot, local_path = await create_snapshot(...)`, so its local
            # `snapshot` stays None and its `finally` deletes NOTHING. Sweep the
            # tree ourselves or leak the parent plus one snapshot per descendant
            # dataset (160+ here) on every failed run.
            await _tc_nested.delete_snapshot_tree(middleware, snapshot, logger=_logger)
            raise

        return snapshot, staging_root

    create_snapshot._truecloud_patched = True
"""

CRUD_BLOCK = """
# TRUECLOUD_PATCH — added by truenas-truecloud-patch/patch/apply.sh
try:
    from middlewared.plugins.cloud import _truecloud_nested as _tc_nested
except ImportError:
    _tc_nested = None

if _tc_nested is not None:
    _tc_orig_validate = CloudTaskServiceMixin._validate

    async def _tc_validate(self, app, verrors, name, data):
        await _tc_orig_validate(self, app, verrors, name, data)

        # Only cloud_backup: staging teardown is wired into cloud_backup.sync's
        # finally. cloudsync would leak bind mounts, so leave its guard intact.
        if getattr(getattr(self, "_config", None), "namespace", "") != "cloud_backup":
            return

        # Drop ONLY the nested-dataset guard. If iX ever rewords the message the
        # filter stops matching, the guard survives, and the option merely stays
        # unavailable -- the safe direction to fail.
        verrors.errors = [
            e for e in verrors.errors
            if not (
                getattr(e, "attribute", "") == f"{name}.snapshot"
                and "no further nesting" in getattr(e, "errmsg", "")
            )
        ]

    CloudTaskServiceMixin._validate = _tc_validate
    CloudTaskServiceMixin._validate._truecloud_patched = True
"""

SYNC_BLOCK = """
# TRUECLOUD_PATCH — added by truenas-truecloud-patch/patch/apply.sh
try:
    from middlewared.plugins.cloud import _truecloud_nested as _tc_nested
except ImportError:
    _tc_nested = None

if _tc_nested is not None:
    _tc_orig_restic_backup = restic_backup

    async def restic_backup(middleware, job, cloud_backup, dry_run=False, rate_limit=None):
        # Our bind mounts pin the ZFS snapshot, so stock's `finally` cannot
        # destroy it (EBUSY) and logs one benign warning. We unmount here and
        # then delete the snapshot for real.
        try:
            return await _tc_orig_restic_backup(middleware, job, cloud_backup, dry_run, rate_limit)
        finally:
            try:
                await _tc_nested.cleanup_task(
                    middleware,
                    f"cloud_backup-{cloud_backup.get('id', 'onetime')}",
                    logger=getattr(middleware, "logger", None),
                )
            except Exception as e:
                middleware.logger.warning("truecloud-patch: staging cleanup failed: %r", e)

    restic_backup._truecloud_patched = True
"""


def patch_file(path, block):
    with open(path, encoding="utf-8") as fh:
        content = fh.read()
    marker = "\n# TRUECLOUD_PATCH"
    idx = content.find(marker)
    base = content[:idx] if idx != -1 else content
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(base.rstrip("\n") + "\n" + block)

b2_ok = restic_ok = False
nested_ok = False

if os.path.exists(b2_path):
    try:
        patch_file(b2_path, B2_BLOCK)
        b2_ok = True
        print(f"OK: Patched b2.py → {b2_path}")
    except Exception as e:
        print(f"WARNING: Failed to patch b2.py: {e}")
else:
    print(f"WARNING: b2.py not found at {b2_path}")

if os.path.exists(restic_path):
    try:
        patch_file(restic_path, RESTIC_BLOCK)
        restic_ok = True
        print(f"OK: Patched restic.py → {restic_path}")
    except Exception as e:
        print(f"WARNING: Failed to patch restic.py: {e}")
else:
    print(f"WARNING: restic.py not found at {restic_path}")

# ── nested-dataset snapshot support ───────────────────────────────────────────
# Order matters: install the traversal machinery FIRST, relax the validation
# guard LAST. If anything fails partway, the guard is still in place and the
# option stays unavailable -- we never expose "guard removed, traversal missing".
nested_detail = ''
if not nested_enabled:
    nested_detail = 'disabled (opt-in; enable with: install.sh --enable-nested-snapshots)'
    print('INFO: Nested-dataset snapshot support is disabled (opt-in feature).')
    print('INFO: Enable with: bash install.sh --enable-nested-snapshots')
else:
    try:
        snapshot_py = os.path.join(cloud_dir, 'snapshot.py')
        crud_py = os.path.join(cloud_dir, 'crud.py')
        nested_dst = os.path.join(cloud_dir, '_truecloud_nested.py')

        missing = [p for p in (snapshot_py, crud_py, sync_path, nested_src) if not os.path.exists(p)]
        if missing:
            raise FileNotFoundError('missing: ' + ', '.join(missing))

        shutil.copyfile(nested_src, nested_dst)   # 1. traversal implementation
        patch_file(snapshot_py, SNAPSHOT_BLOCK)   # 2. build the staging tree
        patch_file(sync_path, SYNC_BLOCK)         # 3. tear it down afterwards
        patch_file(crud_py, CRUD_BLOCK)           # 4. ONLY NOW allow nested tasks

        nested_ok = True
        nested_detail = 'nested-dataset snapshots enabled (staging tree)'
        print(f'OK: Installed nested-snapshot support → {nested_dst}')
        print(f'OK: Patched snapshot.py, sync.py, crud.py → {cloud_dir}')
    except Exception as e:
        nested_detail = f'not applied: {e}'
        print(f'WARNING: Failed to apply nested-snapshot patch: {e}')
        print('WARNING: Stock nesting guard remains; snapshot option stays unavailable')
        print('WARNING: for nested datasets. Existing backups are unaffected.')

patches = {
    'middlewared.rclone.remote.b2': {
        'ok': b2_ok,
        'detail': 'patched on disk in overlay at boot' if b2_ok else 'b2.py not found or write failed',
    },
    'middlewared.plugins.cloud_backup.restic': {
        'ok': restic_ok,
        'detail': 'patched on disk in overlay at boot' if restic_ok else 'restic.py not found or write failed',
    },
    'middlewared.plugins.cloud.nested_snapshot': {
        'ok': nested_ok,
        'detail': nested_detail,
    },
}
payload = {'patched_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'patches': patches}
tmp = status_path + '.tmp'
try:
    with open(tmp, 'w') as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, status_path)
    print('OK: Wrote hook_status.json')
except OSError as e:
    print(f'WARNING: Could not write hook_status.json: {e}')

sys.exit(0 if (b2_ok and restic_ok) else 1)
PYEOF
        then
            _b2_ok=1
            _restic_ok=1
        else
            # Individual results already printed above; exit code 1 means at least one failed.
            true
        fi
fi

# ── Step 2: Angular bundle ────────────────────────────────────────────────────

echo "--- UI patch ---"

# Ensure the webui directory is writable before patch_ui.py tries to create a
# backup and write the patched bundle.  On immutable OS we mount an overlay.
_webui_dir=""
for _d in /usr/share/truenas/webui /usr/share/truenas-ui /var/www/truenas; do
    if [ -d "$_d" ]; then
        _webui_dir="$_d"
        break
    fi
done
if [ -n "$_webui_dir" ]; then
    _ensure_writable "$_webui_dir" "ui" || true   # non-fatal; patch_ui.py reports the error
fi

"$PYTHON" "$PATCH_DIR/patch/patch_ui.py" || echo "WARNING: patch_ui.py exited non-zero; UI dropdown may still show Storj only."

# ── Step 3: deferred middlewared restart (boot runs only) ─────────────────────
# At boot this script is spawned by middlewared, which already imported the
# stock modules — the backend patch is on disk but not in the process. Schedule
# ONE detached restart for after boot settles. Never restart synchronously
# here: this script is a child of middlewared's own job runner, and the later
# ix-* boot units still need midclt to answer.
# Boot context is detected by the parent process being middlewared; manual
# runs (install.sh, recovery) never trigger a restart.
#
# The unit runs wait_restart.sh, which blocks until boot has actually
# settled (systemd job queue drained, docker/apps state terminal) before
# restarting. systemd ordering alone (After=multi-user.target, ≤ v0.0.4)
# fired while ix-reporting and the docker/apps startup were still in flight
# and killed both — apps and dashboard stats stayed down until the next
# boot. No Type=oneshot: a oneshot's start job would hold the boot queue
# open against the `is-system-running --wait` inside the script.

echo "--- deferred restart ---"

if ! grep -aq middlewared "/proc/$PPID/cmdline" 2>/dev/null; then
    echo "Manual run (parent is not middlewared) — no restart scheduled."
elif [ "$_b2_ok" != "1" ] || [ "$_restic_ok" != "1" ]; then
    echo "Backend patch incomplete — no restart scheduled (nothing new to load)."
else
    # A failed unit from an earlier attempt this boot would block systemd-run.
    systemctl reset-failed truecloud-mw-restart.service 2>/dev/null
    if systemd-run --no-block --collect --unit=truecloud-mw-restart \
           /bin/bash "$PATCH_DIR/patch/wait_restart.sh"; then
        echo "OK: Scheduled deferred middlewared restart (unit: truecloud-mw-restart)."
        echo "    It waits for boot to fully settle (apps started, reporting up),"
        echo "    then restarts middlewared so the backend patch actually loads."
    else
        echo "WARNING: Could not schedule deferred restart — backend patch is on disk but NOT loaded."
        echo "  Activate manually: systemctl restart middlewared"
    fi
fi

# ── Done ──────────────────────────────────────────────────────────────────────

echo "=== done ==="
