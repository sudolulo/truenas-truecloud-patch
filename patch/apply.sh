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
VERSION="0.6.1"

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

# The patch has two independent modules, and each retires on its own:
#
#   providers  — B2/S3 credentials for TrueCloud Backup (b2.py, restic.py, UI)
#   nested     — snapshots on datasets that have child datasets (cloud/*.py)
#
# TrueNAS may well ship one natively long before the other, so a single
# all-or-nothing kill switch would silently take a still-needed module down with
# the superseded one. Each module is detected separately and skipped on its own;
# the global kill switch fires only once BOTH are native.

_tc_info=$("$PYTHON" -c "
import inspect, os, sys

result = {'native_b2': 'no', 'native_nested': 'no', 'site_pkg': '', 'mw_dir': ''}

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

# providers: does B2RcloneRemote already carry a real get_restic_config()?
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
                result['native_b2'] = 'yes'
except Exception:
    pass

# nested: stock gates nested datasets with a validation in plugins/cloud/crud.py.
# If that guard is gone, iX removed it, which means they implemented the traversal.
#
# Only look at the STOCK part of the file. Our own CRUD_BLOCK quotes the guard
# message (it filters on it), so scanning the whole file would find the string in
# our own patch and conclude the guard is still there. That happens to be
# harmless today because detection runs before patching, but it makes the probe
# silently order-dependent -- so cut our block off explicitly.
#
# If the file cannot be read we assume 'no' and keep patching: worst case the
# patch declines to apply and the option simply stays unavailable.
try:
    crud = os.path.join(result['mw_dir'], 'plugins', 'cloud', 'crud.py')
    with open(crud, encoding='utf-8', errors='replace') as fh:
        stock_src = fh.read().split('\n# TRUECLOUD_PATCH', 1)[0]
    # The guard message is SPLIT across adjacent string literals in the source:
    #
    #     verrors.add(..., 'This option is only available for datasets that have no further '
    #                      'nesting')
    #
    # Python concatenates those at runtime, so the errmsg is contiguous -- but the
    # SOURCE never contains the whole phrase. A raw search finds nothing, concludes
    # iX removed the guard, and silently skips this module FOREVER. (Caught only by
    # running the probe against real middlewared.)
    #
    # Strip whitespace and quote characters, then match the compacted phrase. That
    # is robust to any wrapping or concatenation style iX may use.
    _drop = str.maketrans('', '', ' \\t\\n\\r' + chr(34) + chr(39))
    if 'nofurthernesting' not in stock_src.translate(_drop):
        result['native_nested'] = 'yes'
except Exception:
    pass

print(result['native_b2'])
print(result['native_nested'])
print(result['site_pkg'])
print(result['mw_dir'])
" 2>/dev/null || printf 'no\nno\n\n\n')

_tc_native_b2=$(printf '%s' "$_tc_info"     | sed -n '1p')
_tc_native_nested=$(printf '%s' "$_tc_info" | sed -n '2p')
SITE_PKG=$(printf '%s' "$_tc_info"          | sed -n '3p')
_MW_DIR=$(printf '%s' "$_tc_info"           | sed -n '4p')

# Nested support is opt-in; if it was never enabled, it cannot be the reason to
# keep the patch alive.
if [ -f "$PATCH_DIR/nested_snapshots_enabled" ]; then
    _NESTED_ENABLED=1
else
    _NESTED_ENABLED=0
fi

# ── compatibility preflight ──────────────────────────────────────────────────
#
# The native probes above ask "has iX made this module unnecessary?". This asks the
# other question, the dangerous one: "has iX changed middleware so that this module
# no longer WORKS?"
#
# middlewared is internal API with no stability contract, and TrueNAS 26 rewrites
# the entire cloud_backup path from async to synchronous. Every block the nested
# module injects is an `async def` wrapping an `await`ed original; on 26 that hands
# sync.py a coroutine where it unpacks a tuple. The backup does not fail cleanly --
# it fails at the point where you needed it.
#
# tools/compat.py records what each module assumes and checks it against the
# middlewared ACTUALLY INSTALLED HERE. A module whose assumptions no longer hold is
# not applied. Stock TrueNAS without a feature beats TrueNAS with a broken one.
#
# Fail direction, deliberately asymmetric:
#   * a definite "assumption violated"  -> disable that module. Strong evidence.
#   * the checker cannot run at all     -> change nothing. That is a tooling glitch,
#     not evidence, and turning it into a disabled module would break working boxes.
_TC_COMPAT_JSON="$PATCH_DIR/incompatible.json"
rm -f "$_TC_COMPAT_JSON"

_tc_incompatible=0
_tc_compat=unknown

# No middlewared directory means the checker has nothing to read -- every module
# would look "broken" because every file is missing, which is the strongest possible
# evidence derived from the weakest possible input. Skip the preflight entirely and
# let the existing "Cannot determine middlewared directory" path handle it.
if [ -z "$_MW_DIR" ]; then
    echo "NOTICE: middlewared directory unknown; skipping the compatibility preflight."
    _tc_compat=$(printf 'unknown\nunknown\n')
else
_tc_compat=$("$PYTHON" - "$PATCH_DIR" "$_MW_DIR" "$_TC_COMPAT_JSON" 2>/dev/null <<'PYEOF' || printf 'unknown\nunknown\n'
import json, os, sys

patch_dir, mw_dir, out_path = sys.argv[1], sys.argv[2], sys.argv[3]

# APPEND, never insert(0) -- see the note by the mw_patch import below. Shadowing
# the stdlib for this interpreter is a much worse failure than not finding compat.
sys.path.append(os.path.join(patch_dir, 'tools'))
try:
    import compat
    result = compat.check_tree(mw_dir)
except Exception:
    print('unknown')
    print('unknown')
    raise SystemExit(0)

def verdict(r):
    # Deliberately NOT exempting 'native' here, unlike the CI matrix.
    #
    # 'native' answers "do we still NEED this module?"; 'ok' answers "is it still
    # SAFE to inject?". They are different questions, and letting native mask a
    # broken assumption conflates them: a future TrueNAS that both reworded the
    # nesting guard (-> native) AND changed the signatures (-> broken) would read
    # as safe, and we would patch it anyway.
    #
    # Refusing to apply is the correct action for BOTH answers -- a native module
    # is unnecessary and a broken one is dangerous -- so the apply path only has to
    # ask whether the assumptions hold. Whether the feature went native is decided
    # separately, by the probes above, and only affects the wording of the notice.
    return 'ok' if r['ok'] else 'broken'

broken = {m: r for m, r in result.items() if verdict(r) == 'broken'}
if broken:
    # The alert source reads this. Written before we print, so a box that is
    # incompatible always has the evidence on disk even if apply.sh dies later.
    try:
        with open(out_path, 'w', encoding='utf-8') as fh:
            json.dump(broken, fh, indent=2)
    except OSError:
        pass

print(verdict(result['providers']))
print(verdict(result['nested']))
PYEOF
)
fi

_tc_compat_providers=$(printf '%s' "$_tc_compat" | sed -n '1p')
_tc_compat_nested=$(printf '%s' "$_tc_compat"    | sed -n '2p')

# Is either module still doing something useful -- and can it still be applied?
_providers_needed=1
[ "$_tc_native_b2" = "yes" ] && _providers_needed=0

_nested_needed=0
if [ "$_NESTED_ENABLED" = "1" ] && [ "$_tc_native_nested" != "yes" ]; then
    _nested_needed=1
fi

# The native checks above have already zeroed _*_needed for anything TrueNAS now
# does itself, and printed the (good) news. Only complain about a module that is
# still NEEDED and no longer fits -- otherwise a version that took a feature native
# AND reshaped the module would be announced as "NOT COMPATIBLE", which is alarming
# and false.
if [ "$_tc_compat_providers" = "broken" ] && [ "$_providers_needed" = "1" ]; then
    echo "WARNING: truecloud-patch is NOT COMPATIBLE with this TrueNAS version."
    echo "WARNING:   The B2/S3 providers module will NOT be applied. TrueCloud is"
    echo "WARNING:   left stock, so B2/S3 tasks will not run until this is fixed."
    echo "WARNING:   Details: $_TC_COMPAT_JSON"
    _providers_needed=0
    _tc_incompatible=1
fi

if [ "$_tc_compat_nested" = "broken" ] && [ "$_nested_needed" = "1" ]; then
    echo "WARNING: truecloud-patch's nested-snapshot module is NOT COMPATIBLE with"
    echo "WARNING:   this TrueNAS version and will NOT be applied. Backups still"
    echo "WARNING:   run; datasets nested under the target are not included."
    echo "WARNING:   Details: $_TC_COMPAT_JSON"
    _nested_needed=0
    _tc_incompatible=1
fi

_tc_unmount_overlays() {
    for _tag in mw ui; do
        if mount | grep -qF "truecloud-${_tag} on "; then
            _mnt=$(mount | grep "truecloud-${_tag} on " | awk '{print $3}' | head -1)
            if umount "$_mnt" 2>/dev/null; then
                echo "NOTICE: Unmounted overlay on $_mnt"
            fi
        fi
    done
}

# INCOMPATIBLE is not the same as RETIRED, and must never take the same exit.
#
# The kill switch below is permanent -- apply.sh checks for it and returns early on
# every future boot -- and only install.sh removes it, NOT update.sh. That is right
# for retirement ("TrueNAS does this natively now; stop forever"), and catastrophic
# for incompatibility: on TrueNAS 26 the providers module fails its assumptions and
# nested is opt-out by default, so BOTH would be zero, the kill switch would fire,
# and the very release that fixes 26 could never re-enable itself. The user would
# run `bash update.sh` -- exactly what the update alert tells them to do -- and the
# patch would stay dead, silently, with their B2 backups off.
#
# So: incompatible means "apply nothing THIS boot, and try again next boot". The
# fix ships, update.sh checks it out, the next boot re-runs the preflight, the
# assumptions hold, and the patch comes back by itself.
if [ "$_tc_incompatible" = "1" ] && [ "$_providers_needed" = "0" ] && [ "$_nested_needed" = "0" ]; then
    echo "NOTICE: Nothing can be applied on this TrueNAS version — see the WARNINGs above."
    echo "NOTICE: The kill switch is deliberately NOT set: this is an incompatibility,"
    echo "NOTICE: not a retirement. Install a release that supports this TrueNAS"
    echo "NOTICE:   bash $PATCH_DIR/update.sh"
    echo "NOTICE: and the patch will re-apply itself on the next boot."
    _tc_unmount_overlays
    echo "=== done ==="
    exit 0
fi

if [ "$_providers_needed" = "0" ] && [ "$_nested_needed" = "0" ]; then
    echo "NOTICE: Nothing left for truecloud-patch to do:"
    [ "$_tc_native_b2" = "yes" ] && echo "NOTICE:   - TrueNAS now provides native B2 restic support."
    if [ "$_tc_native_nested" = "yes" ]; then
        echo "NOTICE:   - TrueNAS now handles snapshots on nested datasets natively."
    elif [ "$_NESTED_ENABLED" = "0" ]; then
        echo "NOTICE:   - Nested-dataset snapshots are not enabled (opt-in)."
    fi
    echo "NOTICE: Setting kill switch; patching will be skipped on all future boots."
    echo "NOTICE: Run the following to fully remove the patch:"
    echo "NOTICE:   bash $PATCH_DIR/uninstall.sh"
    touch "$PATCH_DIR/disabled"
    _tc_unmount_overlays
    echo "=== done ==="
    exit 0
fi

if [ "$_providers_needed" = "0" ]; then
    echo "NOTICE: TrueNAS now provides native B2 restic support — the providers module"
    echo "NOTICE: is superseded and will be skipped. The nested-snapshot module is still"
    echo "NOTICE: active, so the patch stays installed."
fi
if [ "$_NESTED_ENABLED" = "1" ] && [ "$_tc_native_nested" = "yes" ]; then
    echo "NOTICE: TrueNAS now handles nested-dataset snapshots natively — that module is"
    echo "NOTICE: superseded and will be skipped. You can drop --enable-nested-snapshots."
fi

# ── Step 1: backend patch ─────────────────────────────────────────────────────

echo "--- backend patch ---"

_backend_ok=0

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

    # Each module is applied only if it is still needed. _providers_needed and
    # _nested_needed were computed above (native-support detection + the opt-in
    # marker), so one module going native never disables the other.
        # ── patch b2.py + restic.py + nested-snapshot + hook_status.json ────────
        # (single subprocess: PREINIT has a tight timeout budget)
        if "$PYTHON" - "$_B2_PY" "$_RESTIC_PY" "$PATCH_DIR/hook_status.json" \
                      "$_CLOUD_DIR" "$_SYNC_PY" "$_NESTED_SRC" \
                      "$_providers_needed" "$_nested_needed" "$_NESTED_ENABLED" \
                      "$_tc_native_nested" << 'PYEOF'
import json, os, shutil, sys, time

b2_path, restic_path, status_path = sys.argv[1], sys.argv[2], sys.argv[3]
cloud_dir, sync_path, nested_src = sys.argv[4], sys.argv[5], sys.argv[6]
providers_needed = sys.argv[7] == "1"
nested_needed = sys.argv[8] == "1"
nested_enabled = sys.argv[9] == "1"
nested_native = sys.argv[10] == "yes"

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

# ── nested blocks: one core, two wrappers ─────────────────────────────────────
#
# TrueNAS <= 25.10 has an ASYNC cloud_backup path; TrueNAS 26 rewrote it SYNCHRONOUS
# (`middleware.call_sync` throughout, no awaits). An `async def` wrapper on 26 hands
# sync.py a coroutine where it unpacks a tuple, and a `def` wrapper on 25.10 blocks
# the event loop. So each block is assembled from:
#
#   * a CORE, written once, synchronous, using middleware.call_sync -- which is safe
#     from a worker thread and deadlocks on the event loop; and
#   * a WRAPPER matching the stock function's own flavour, chosen at apply time by
#     reading whether the installed middlewared declares it `async def`.
#
# On <= 25.10 the async wrapper hops to a thread via `await middleware.run_in_thread`
# -- exactly the thread call_sync needs. On 26 the stock function is already running
# in middlewared's thread pool (its own code calls call_sync), so the sync wrapper
# calls the core directly.
#
# The logic that matters -- snapshots, bind mounts, failure modes -- exists once.
# An async twin would mean every future fix had to land twice, and the one that got
# missed would be the one that eats a backup.

_NESTED_IMPORT = """
# TRUECLOUD_PATCH — added by truenas-truecloud-patch/patch/apply.sh
try:
    from middlewared.plugins.cloud import _truecloud_nested as _tc_nested
except ImportError:
    _tc_nested = None
"""

SNAPSHOT_CORE = _NESTED_IMPORT + """
if _tc_nested is not None:
    _tc_orig_create_snapshot = create_snapshot

    def _tc_stage(middleware, path, name, snapshot, snap_path):
        # Synchronous, and always called from a worker thread (see above).
        #
        # ONLY cloud_backup. Bail out before touching anything otherwise.
        #
        # create_snapshot is module-global in plugins/cloud/snapshot.py and is
        # imported by cloud_sync.py as well as cloud_backup/sync.py -- so this
        # wrapper sits in the path of every rclone/Storj CloudSync task with
        # snapshot=true, not just ours. Two consequences, and the second is worse:
        #
        #   * everything below is a NEW failure mode for tasks that worked before we
        #     were installed. A `pool.dataset.query` that errors would break a
        #     CloudSync job we have no business touching.
        #   * if a CloudSync task ever were staged, nothing would ever tear it down:
        #     the teardown is wired into cloud_backup's restic_backup finally, and
        #     CRUD_BLOCK deliberately leaves CloudSync's nesting guard intact. The
        #     bind mounts would pin the snapshot forever.
        #
        # cloud_backup names its snapshot "cloud_backup-<id>"; cloud_sync names it
        # "cloud_sync-<id>"; the stock default is "cloud_task-onetime". Anything that
        # is not ours gets stock behaviour, untouched, with no extra middleware call.
        if not name.startswith("cloud_backup"):
            return snapshot, snap_path

        _logger = getattr(middleware, "logger", None)
        try:
            # Enumerate datasets AFTER the snapshot, never before. The snapshot is
            # the point-in-time truth; a list read beforehand could miss a dataset
            # created in the gap, which the recursive snapshot WOULD capture but
            # our staging plan would not -- silently omitting it from the backup.
            # Read afterwards, an unsnapshotted dataset instead trips the isdir()
            # check in plan_staging and fails the run loudly. Loud beats silent.
            # query_filesystems() reads ZFS directly. It deliberately does NOT use
            # pool.dataset.query: that applies a visibility policy and hides
            # TrueNAS-internal datasets (ix-apps/*, .system/*, .ix-virt/*) -- 84 of
            # 270 on a real pool, including live app data. Staging from the filtered
            # view omits them silently, which is the one thing this must never do.
            datasets = _tc_nested.query_filesystems(middleware)
            # OUR copy of get_dataset_recursive, not the host module's: TrueNAS 26
            # deleted that helper (create_snapshot uses filesystem.statfs now), so
            # calling it out of the module namespace is a NameError there.
            dataset, nested = _tc_nested.get_dataset_recursive(datasets, path)

            if not nested:
                # No children: stock behaviour, untouched. Stock's `finally` owns
                # the snapshot from here (its non-recursive delete is correct,
                # because a non-nested snapshot has no children).
                return snapshot, snap_path

            staging_root = _tc_nested.stage_nested(
                middleware, path, snapshot,
                dataset["name"], dataset["properties"]["mountpoint"]["value"],
                name, datasets, logger=_logger,
            )
        except Exception:
            # The snapshot exists, but this exception means sync.py never completes
            # `snapshot, local_path = create_snapshot(...)`, so its local `snapshot`
            # stays None and its `finally` deletes NOTHING. Sweep the tree ourselves
            # or leak the parent plus one snapshot per descendant dataset (160+ here)
            # on every failed run.
            _tc_nested.delete_snapshot_tree(middleware, snapshot, logger=_logger)
            raise

        return snapshot, staging_root
"""

SNAPSHOT_ASYNC = SNAPSHOT_CORE + """
    async def create_snapshot(middleware, path, name="cloud_task-onetime"):
        snapshot, snap_path = await _tc_orig_create_snapshot(middleware, path, name)
        return await middleware.run_in_thread(
            _tc_stage, middleware, path, name, snapshot, snap_path
        )

    create_snapshot._truecloud_patched = True
"""

SNAPSHOT_SYNC = SNAPSHOT_CORE + """
    def create_snapshot(middleware, path, name="cloud_task-onetime"):
        snapshot, snap_path = _tc_orig_create_snapshot(middleware, path, name)
        return _tc_stage(middleware, path, name, snapshot, snap_path)

    create_snapshot._truecloud_patched = True
"""

_CRUD_FILTER = """
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

CRUD_ASYNC = _NESTED_IMPORT + """
if _tc_nested is not None:
    _tc_orig_validate = CloudTaskServiceMixin._validate

    async def _tc_validate(self, app, verrors, name, data):
        await _tc_orig_validate(self, app, verrors, name, data)
""" + _CRUD_FILTER

CRUD_SYNC = _NESTED_IMPORT + """
if _tc_nested is not None:
    _tc_orig_validate = CloudTaskServiceMixin._validate

    def _tc_validate(self, app, verrors, name, data):
        _tc_orig_validate(self, app, verrors, name, data)
""" + _CRUD_FILTER

# *args/**kwargs, not the stock signature spelled out.
#
# 24.10 and 25.04 have `restic_backup(middleware, job, cloud_backup, dry_run)`;
# 25.10 added `rate_limit`. Naming them and forwarding all five raised
# `TypeError: takes 4 positional arguments but 5 were given` on every nested backup
# on the two older releases. Forwarding whatever we were handed makes this wrapper
# indifferent to iX adding or dropping a trailing parameter -- which they have now
# done twice.
#
# Our bind mounts pin the ZFS snapshot, so stock's `finally` cannot destroy it
# (EBUSY) and logs one benign warning. We unmount here and then delete it for real.
SYNC_ASYNC = _NESTED_IMPORT + """
if _tc_nested is not None:
    _tc_orig_restic_backup = restic_backup

    async def restic_backup(middleware, job, cloud_backup, *args, **kwargs):
        try:
            return await _tc_orig_restic_backup(middleware, job, cloud_backup, *args, **kwargs)
        finally:
            try:
                # logger= must be passed here too, exactly as the sync variant does.
                # run_in_thread forwards **kwargs (functools.partial), and without it
                # cleanup_task gets logger=None -- so every teardown warning ("could
                # not unmount X") is silently swallowed on <= 25.10, which is most
                # boxes. The two wrappers must differ ONLY in how they reach the core.
                await middleware.run_in_thread(
                    _tc_nested.cleanup_task,
                    middleware,
                    f"cloud_backup-{cloud_backup.get('id', 'onetime')}",
                    logger=getattr(middleware, "logger", None),
                )
            except Exception as e:
                middleware.logger.warning("truecloud-patch: staging cleanup failed: %r", e)

    restic_backup._truecloud_patched = True
"""

SYNC_SYNC = _NESTED_IMPORT + """
if _tc_nested is not None:
    _tc_orig_restic_backup = restic_backup

    def restic_backup(middleware, job, cloud_backup, *args, **kwargs):
        try:
            return _tc_orig_restic_backup(middleware, job, cloud_backup, *args, **kwargs)
        finally:
            try:
                _tc_nested.cleanup_task(
                    middleware,
                    f"cloud_backup-{cloud_backup.get('id', 'onetime')}",
                    logger=getattr(middleware, "logger", None),
                )
            except Exception as e:
                middleware.logger.warning("truecloud-patch: staging cleanup failed: %r", e)

    restic_backup._truecloud_patched = True
"""


# Single implementation of the block apply/revert logic (patch/mw_patch.py), so
# uninstall.sh and apply.sh cannot drift apart. Fail-safe: if it cannot be
# imported, skip the backend patch entirely -- middlewared then starts stock,
# which is the whole design principle of this script.
# APPEND, never insert(0): this dir would otherwise take precedence over the
# stdlib for this interpreter, so a future patch/json.py (say) would shadow the
# real json module and break the boot. Appending fails safe -- worst case our
# import misses and the backend patch is skipped.
sys.path.append(os.path.dirname(nested_src))
try:
    from mw_patch import patch_file, revert_nested
except ImportError as _e:
    print(f'WARNING: cannot import patch/mw_patch.py ({_e}) — skipping backend patch.')
    print('WARNING: middlewared will start with stock (unpatched) modules.')
    sys.exit(1)

# .../middlewared/plugins/cloud -> .../middlewared
mw_dir = os.path.dirname(os.path.dirname(cloud_dir))

b2_ok = restic_ok = False
nested_ok = False

# ── module: providers (B2/S3) ─────────────────────────────────────────────────
# Skipped entirely once TrueNAS ships native B2 restic support. That must not
# take the nested module down with it, so the two are gated independently.
providers_detail = ''
if not providers_needed:
    providers_detail = 'superseded: TrueNAS provides native B2 restic support'
    print('INFO: Providers module skipped — TrueNAS now supports B2 natively.')
else:
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
    providers_detail = (
        'patched on disk in overlay at boot' if (b2_ok and restic_ok)
        else 'b2.py/restic.py not found or write failed'
    )

# ── module: nested-dataset snapshots ──────────────────────────────────────────
# Order matters: install the traversal machinery FIRST, relax the validation
# guard LAST. If anything fails partway, the guard is still in place and the
# option stays unavailable -- we never expose "guard removed, traversal missing".
nested_detail = ''
if not nested_needed:
    # Not just "skip": actively revert. The overlay lives for the whole boot, so a
    # previously-applied patch is still sitting there and middlewared would
    # re-import it on restart. See revert_nested().
    if not nested_enabled:
        nested_detail = 'disabled (opt-in; enable with: install.sh --enable-nested-snapshots)'
        print('INFO: Nested-dataset snapshot support is disabled (opt-in feature).')
    elif nested_native:
        nested_detail = 'superseded: TrueNAS handles nested-dataset snapshots natively'
        print('INFO: Nested module skipped — TrueNAS now handles nesting natively.')
    else:
        nested_detail = 'not needed'
        print('INFO: Nested module skipped.')

    reverted = revert_nested(mw_dir)
    if reverted:
        print('OK: Reverted a previously-applied nested patch (' + ', '.join(reverted) + ').')
        print('    The stock nesting guard is restored once middlewared restarts.')
        nested_detail += ' — previous patch reverted'

    if not nested_enabled:
        print('INFO: Enable with: bash install.sh --enable-nested-snapshots')
else:
    try:
        snapshot_py = os.path.join(cloud_dir, 'snapshot.py')
        crud_py = os.path.join(cloud_dir, 'crud.py')
        nested_dst = os.path.join(cloud_dir, '_truecloud_nested.py')

        missing = [p for p in (snapshot_py, crud_py, sync_path, nested_src) if not os.path.exists(p)]
        if missing:
            raise FileNotFoundError('missing: ' + ', '.join(missing))

        # Which flavour of cloud_backup is installed? <= 25.10 is async; TrueNAS 26
        # rewrote it synchronous. Inject the wrapper that matches: an `async def` on
        # 26 hands sync.py a coroutine where it unpacks a tuple, and a plain `def` on
        # 25.10 blocks the event loop.
        #
        # None means the three stock functions disagree, or one could not be read.
        # Refuse rather than guess -- a half-converted middleware is one this patch
        # has never seen, and guessing wrong there costs a backup, not a feature.
        # nested_src is <repo>/patch/truecloud_nested.py, so tools/ is its sibling.
        # APPEND, never insert(0) -- shadowing the stdlib for this interpreter is a
        # far worse failure than not finding compat.
        sys.path.append(
            os.path.join(os.path.dirname(os.path.dirname(nested_src)), 'tools')
        )
        import compat
        _flavour = compat.async_flavour_tree(mw_dir)
        if _flavour is None:
            raise RuntimeError(
                'cannot tell whether this TrueNAS cloud_backup path is async or '
                'sync (the wrapped functions disagree, or could not be read)'
            )

        _snapshot_block = SNAPSHOT_ASYNC if _flavour else SNAPSHOT_SYNC
        _sync_block     = SYNC_ASYNC     if _flavour else SYNC_SYNC
        _crud_block     = CRUD_ASYNC     if _flavour else CRUD_SYNC

        shutil.copyfile(nested_src, nested_dst)   # 1. traversal implementation
        patch_file(snapshot_py, _snapshot_block)  # 2. build the staging tree
        patch_file(sync_path, _sync_block)        # 3. tear it down afterwards
        patch_file(crud_py, _crud_block)          # 4. ONLY NOW allow nested tasks

        print('OK: cloud_backup is %s; injected the matching wrappers.'
              % ('async (TrueNAS <= 25.10)' if _flavour else 'synchronous (TrueNAS 26+)'))

        nested_ok = True
        nested_detail = 'nested-dataset snapshots enabled (staging tree)'
        print(f'OK: Installed nested-snapshot support → {nested_dst}')
        print(f'OK: Patched snapshot.py, sync.py, crud.py → {cloud_dir}')
    except Exception as e:
        nested_detail = f'not applied: {e}'
        print(f'WARNING: Failed to apply nested-snapshot patch: {e}')
        print('WARNING: Stock nesting guard remains; snapshot option stays unavailable')
        print('WARNING: for nested datasets. Existing backups are unaffected.')

# One entry per MODULE, not per file. `ok` means "nothing is wrong", so a module
# that is inactive (superseded, or opt-in and off) is ok -- reporting a disabled
# opt-in feature as FAIL would make `create_task.py verify` fail on a default
# install. `active` says whether the module is doing anything.
# ── update-available alert ────────────────────────────────────────────────────
# Dropped into middlewared/alert/source/, where middlewared discovers and polls it
# natively — no cron, no timer. It only raises an alert for releases that actually
# changed something: a docs-only release is ignored (see tools/release_notes.py).
alert_ok = False
alert_detail = ''
_patch_dir = os.path.dirname(os.path.dirname(nested_src))
alert_src = os.path.join(os.path.dirname(nested_src), 'alert_source.py')
alert_dst = os.path.join(mw_dir, 'alert', 'source', 'truecloud_patch_update.py')

if os.path.exists(os.path.join(_patch_dir, 'update_alerts_disabled')):
    alert_detail = 'disabled (update_alerts_disabled)'
    try:
        os.unlink(alert_dst)
        print('OK: Removed update alert (disabled).')
    except OSError:
        pass
elif not os.path.exists(alert_src):
    alert_detail = 'alert_source.py not found'
    print(f'WARNING: {alert_src} missing — no update alert.')
else:
    try:
        with open(alert_src, encoding='utf-8') as fh:
            _body = fh.read()

        # repr() so ANY path becomes a valid Python literal -- a directory
        # containing a quote or backslash would otherwise produce a syntax error.
        _body = _body.replace('"@PATCH_DIR@"', repr(_patch_dir))

        # COMPILE BEFORE WRITING. middlewared's alert.load() imports every file in
        # alert/source/ with NO try/except, and it runs at startup -- a module that
        # raises on import takes middlewared's setup down with it. An uninstalled
        # alert is a missing convenience; a broken one is a broken box.
        compile(_body, alert_dst, 'exec')

        with open(alert_dst, 'w', encoding='utf-8') as fh:
            fh.write(_body)
        alert_ok = True
        alert_detail = 'update alert installed'
        print(f'OK: Installed update alert → {alert_dst}')
    except Exception as e:
        alert_detail = f'not applied: {e}'
        print(f'WARNING: could not install update alert: {e}')
        print('WARNING: no update alert; everything else is unaffected.')

patches = {
    'providers': {
        'ok': (not providers_needed) or bool(b2_ok and restic_ok),
        'active': providers_needed,
        'detail': providers_detail,
    },
    'nested_snapshots': {
        'ok': (not nested_needed) or nested_ok,
        'active': nested_needed,
        'detail': nested_detail,
    },
    'update_alert': {
        'ok': True,          # never a failure: it is a convenience, not a patch
        'active': alert_ok,
        'detail': alert_detail,
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

# Exit code tells the caller whether a middlewared restart is still worth doing:
#
#   0  every module that was needed applied cleanly
#   2  PARTIAL -- one module failed but another landed, so there IS something new
#      on disk waiting to be loaded
#   1  nothing landed; a restart would accomplish nothing
#
# Collapsing 2 into 1 would mean a failing providers patch suppresses the restart
# that a freshly-applied nested patch needs, leaving it on disk and never loaded.
_providers_done = (not providers_needed) or bool(b2_ok and restic_ok)
_nested_done = (not nested_needed) or nested_ok
_landed = (providers_needed and b2_ok and restic_ok) or (nested_needed and nested_ok)

if _providers_done and _nested_done:
    sys.exit(0)
sys.exit(2 if _landed else 1)
PYEOF
        then
            _backend_ok=1
        else
            _rc=$?
            if [ "$_rc" = "2" ]; then
                # One module failed, but another was applied and still needs loading.
                _backend_ok=1
                echo "WARNING: a module failed to apply; the other landed and will be loaded."
            else
                _backend_ok=0
            fi
        fi
fi

# ── Step 2: Angular bundle ────────────────────────────────────────────────────
# Belongs to the providers module (it widens the credential dropdown), so it is
# skipped along with it once TrueNAS supports B2 natively.

echo "--- UI patch ---"

if [ "$_providers_needed" = "0" ]; then
    echo "Skipped — providers module superseded by native B2 support."
else
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
fi

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

# Restart when ANY still-needed backend module landed (_backend_ok, incl. the
# partial case). Keying this off the providers module alone would skip the restart
# on a box where B2 has gone native but the nested module was freshly patched —
# leaving it on disk and never loaded.
#
# "No module active at all" cannot reach here: that is the kill-switch branch
# above, which exits.
if ! grep -aq middlewared "/proc/$PPID/cmdline" 2>/dev/null; then
    echo "Manual run (parent is not middlewared) — no restart scheduled."
elif [ "$_backend_ok" != "1" ]; then
    echo "Nothing landed on disk — no restart scheduled (nothing new to load)."
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
