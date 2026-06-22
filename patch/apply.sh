#!/bin/bash
# patch/apply.sh — registered as a TrueNAS PREINIT initshutdownscript.
#
# Runs on every boot BEFORE middlewared starts, so patches land before
# the first Python process for middlewared is created.
#
# TrueNAS updates replace /usr/ entirely; this script re-applies two patches:
#
#   1. Backend — b2.py and restic.py are patched directly in the overlay.
#
#   2. Angular JS bundle — Widens the TrueCloud Backup credential dropdown
#                          from Storj-only to include S3 and B2.
#
# Design principle: every step is independently fail-safe.
# A failed patch logs a warning and continues; middlewared always starts.
# Never use `set -e` in a PREINIT script.

# Derive PATCH_DIR from this script's location (parent of the patch/ directory).
PATCH_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$PATCH_DIR/apply.log"
VERSION="0.0.3"

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
# PREINIT script recreates it on every boot before middlewared starts.
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

        # ── patch b2.py + restic.py + hook_status.json (single subprocess) ──────
        if "$PYTHON" - "$_B2_PY" "$_RESTIC_PY" "$PATCH_DIR/hook_status.json" << 'PYEOF'
import json, os, sys, time

b2_path, restic_path, status_path = sys.argv[1], sys.argv[2], sys.argv[3]

B2_BLOCK = """
# TRUECLOUD_PATCH — added by truenas-truecloud-patch/patch/apply.sh
def _tc_get_restic_config(task):
    p = task["credentials"]["provider"]
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

def patch_file(path, block):
    with open(path, encoding="utf-8") as fh:
        content = fh.read()
    marker = "\n# TRUECLOUD_PATCH"
    idx = content.find(marker)
    base = content[:idx] if idx != -1 else content
    with open(path, "w", encoding="utf-8") as fh:
        fh.write(base.rstrip("\n") + "\n" + block)

b2_ok = restic_ok = False

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

patches = {
    'middlewared.rclone.remote.b2': {
        'ok': b2_ok,
        'detail': 'patched on disk in overlay at boot' if b2_ok else 'b2.py not found or write failed',
    },
    'middlewared.plugins.cloud_backup.restic': {
        'ok': restic_ok,
        'detail': 'patched on disk in overlay at boot' if restic_ok else 'restic.py not found or write failed',
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

# ── Done ──────────────────────────────────────────────────────────────────────

echo "=== done ==="
