#!/bin/bash
# patch/apply.sh — registered as a TrueNAS PREINIT initshutdownscript.
#
# Runs on every boot BEFORE middlewared starts, so patches land before
# the first Python process for middlewared is created.
#
# TrueNAS updates replace /usr/ entirely; this script re-applies two patches:
#
#   1. Backend — b2.py and restic.py are patched directly in the overlay.
#      sitecustomize.py is also installed as belt-and-suspenders.
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

# Rotate log at 512 KB to avoid unbounded growth on a system volume.
# Keep two prior generations (.1 and .2) so the last three boots are always available.
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 524288 ]; then
    [ -f "${LOG}.1" ] && mv "${LOG}.1" "${LOG}.2"
    mv "$LOG" "${LOG}.1"
fi

exec >> "$LOG" 2>&1
echo "=== $(date -Iseconds) ==="

# Kill switch: if this file exists, skip all patching and exit cleanly.
# Recovery: touch "$PATCH_DIR/disabled" (then reboot or restart middlewared).
if [ -f "$PATCH_DIR/disabled" ]; then
    echo "Kill switch active ($PATCH_DIR/disabled exists) — patch not applied."
    echo "To re-enable: rm $PATCH_DIR/disabled"
    echo "=== done ==="
    exit 0
fi

# On TrueNAS 25.x+, /usr is an immutable read-only filesystem.
# This function mounts a writable overlayfs on $1 using /run (tmpfs) for the
# upper/work dirs.  The overlay is volatile per boot; this PREINIT script
# recreates it on every boot before middlewared starts.
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
        echo "OK: Mounted writable overlay on $dir (immutable filesystem)"
        return 0
    fi
    echo "WARNING: $dir is read-only and overlay mount failed."
    return 1
}

# Find the Python interpreter that middlewared actually uses.
# On TrueNAS SCALE, /usr/bin/middlewared is usually a Python entry-point script
# with a shebang pointing at the right interpreter (system or venv).
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

    # Verify the chosen interpreter can actually import middlewared.
    # Use >&2 so this message goes to stderr, not captured by $(...) substitution.
    if ! "$py" -c "import middlewared" 2>/dev/null; then
        echo "WARNING: '$py' cannot import middlewared; falling back to python3" >&2
        py="python3"
        if ! "$py" -c "import middlewared" 2>/dev/null; then
            echo "WARNING: 'python3' also cannot import middlewared; sitecustomize.py may be installed in the wrong location" >&2
        fi
    fi

    echo "$py"
}

# ── Step 1: backend patch ─────────────────────────────────────────────────────

echo "--- backend patch ---"

PYTHON=$(find_mw_python)
echo "Using Python: $PYTHON"

# Derive site-packages from where middlewared actually lives.
# On TrueNAS 25.x, middlewared is in /usr/lib/python3/dist-packages/, while
# getsitepackages()[0] typically returns /usr/local/lib/python3.11/dist-packages/
# — the wrong directory.  Using middlewared.__file__ ensures we install
# sitecustomize.py and patch files in the directory Python will actually read.
SITE_PKG=$("$PYTHON" -c "
import os
try:
    import middlewared
    # e.g. /usr/lib/python3/dist-packages/middlewared/__init__.py
    #   -> /usr/lib/python3/dist-packages/
    print(os.path.dirname(os.path.dirname(os.path.abspath(middlewared.__file__))))
except ImportError:
    import site
    print(site.getsitepackages()[0])
" 2>/dev/null || true)

# MW_DIR is the middlewared package directory itself (one level below SITE_PKG).
_MW_DIR=$("$PYTHON" -c "
import os
try:
    import middlewared
    print(os.path.dirname(os.path.abspath(middlewared.__file__)))
except ImportError:
    pass
" 2>/dev/null || true)

_b2_ok=0
_restic_ok=0

if [ -z "$SITE_PKG" ]; then
    echo "WARNING: Cannot determine site-packages directory; skipping backend patch."
    echo "  Run: $PYTHON -c \"import site; print(site.getsitepackages())\""
else
    _can_install=true
    # On immutable OS, ensure site-packages is writable via overlay before
    # attempting any writes.
    if ! _ensure_writable "$SITE_PKG" "sc"; then
        _can_install=false
    fi

    # Back up any pre-existing sitecustomize.py that isn't ours.
    if [ "$_can_install" = true ] && \
       [ -f "$SITE_PKG/sitecustomize.py" ] && \
       ! grep -q "truecloud-patch" "$SITE_PKG/sitecustomize.py" 2>/dev/null; then
        if cp "$SITE_PKG/sitecustomize.py" \
              "$SITE_PKG/sitecustomize.py.pre-truecloud-patch"; then
            echo "OK: Backed up existing sitecustomize.py"
        else
            echo "WARNING: Could not back up existing sitecustomize.py; skipping install to avoid data loss."
            _can_install=false
        fi
    fi

    if [ "$_can_install" = true ]; then
        # Substitute PATCH_DIR into the source so sitecustomize.py knows where
        # to write hook_status.json and check the kill switch at runtime.
        _sc_tmp="$SITE_PKG/sitecustomize.py.truecloud-tmp"
        if TRUECLOUD_PATCH_DIR="$PATCH_DIR" \
               "$PYTHON" -c "
import os, sys
d = os.environ['TRUECLOUD_PATCH_DIR']
with open(d + '/patch/sitecustomize.py', encoding='utf-8') as fh:
    sys.stdout.write(fh.read().replace('/data/truecloud-patch', d))
" > "$_sc_tmp" && mv "$_sc_tmp" "$SITE_PKG/sitecustomize.py"; then
            echo "OK: Installed sitecustomize.py → $SITE_PKG/sitecustomize.py"
        else
            rm -f "$_sc_tmp"
            echo "WARNING: Failed to write $SITE_PKG/sitecustomize.py (permission error?)"
        fi
    fi

    # ── Direct file patching ──────────────────────────────────────────────────
    # Patch b2.py and restic.py directly in the overlay (primary approach).
    # Each run strips any existing TRUECLOUD_PATCH block and rewrites it fresh,
    # so a bugfix in the block takes effect immediately on the next apply.sh run
    # without needing to manually clear the overlay.

    if [ -n "$_MW_DIR" ] && [ "$_can_install" = true ]; then
        _B2_PY="$_MW_DIR/rclone/remote/b2.py"
        _RESTIC_PY="$_MW_DIR/plugins/cloud_backup/restic.py"

        # ── b2.py ─────────────────────────────────────────────────────────────
        if [ -f "$_B2_PY" ]; then
            if "$PYTHON" - "$_B2_PY" << 'PYEOF'
import sys

BLOCK = """
# TRUECLOUD_PATCH — added by truenas-truecloud-patch/patch/apply.sh
def _tc_get_restic_config(task):
    p = task["credentials"]["provider"]
    return "", {"B2_ACCOUNT_ID": p["account"], "B2_ACCOUNT_KEY": p["key"]}

if "get_restic_config" not in B2RcloneRemote.__dict__:
    B2RcloneRemote.get_restic_config = staticmethod(_tc_get_restic_config)
    B2RcloneRemote.restic = True
"""

path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    content = fh.read()

marker = "\n# TRUECLOUD_PATCH"
idx = content.find(marker)
base = content[:idx] if idx != -1 else content
patched = base.rstrip("\n") + "\n" + BLOCK

with open(path, "w", encoding="utf-8") as fh:
    fh.write(patched)
PYEOF
            then
                echo "OK: Patched b2.py → $_B2_PY"
                _b2_ok=1
            else
                echo "WARNING: Failed to patch b2.py"
            fi
        else
            echo "WARNING: b2.py not found at $_B2_PY"
        fi

        # ── restic.py ─────────────────────────────────────────────────────────
        if [ -f "$_RESTIC_PY" ]; then
            if "$PYTHON" - "$_RESTIC_PY" << 'PYEOF'
import sys

BLOCK = """
# TRUECLOUD_PATCH — added by truenas-truecloud-patch/patch/apply.sh
_tc_orig_get_restic_config = get_restic_config

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
        # Strip stray leading slash: b2:/bucket -> b2:bucket
        if rest.startswith("/") and not rest.startswith("//"):
            rest = rest[1:]
            changed = True
        # restic 0.16.x B2 uses colon to separate bucket from path:
        #   b2:bucket:prefix  (not b2:bucket/prefix)
        # middlewared builds the slash form; fix the separator.
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

path = sys.argv[1]
with open(path, encoding="utf-8") as fh:
    content = fh.read()

marker = "\n# TRUECLOUD_PATCH"
idx = content.find(marker)
base = content[:idx] if idx != -1 else content
patched = base.rstrip("\n") + "\n" + BLOCK

with open(path, "w", encoding="utf-8") as fh:
    fh.write(patched)
PYEOF
            then
                echo "OK: Patched restic.py → $_RESTIC_PY"
                _restic_ok=1
            else
                echo "WARNING: Failed to patch restic.py"
            fi
        else
            echo "WARNING: restic.py not found at $_RESTIC_PY"
        fi
    elif [ -z "$_MW_DIR" ]; then
        echo "WARNING: Cannot determine middlewared directory; skipping direct file patch."
    fi

    # Write hook_status.json so 'verify' reflects the current patch state
    # without requiring a backup run to trigger the import hook.
    "$PYTHON" -c "
import json, os, sys, time
b2_ok = sys.argv[1] == '1'
restic_ok = sys.argv[2] == '1'
patches = {
    'middlewared.rclone.remote.b2': {
        'ok': b2_ok,
        'detail': ('patched on disk in overlay at boot' if b2_ok
                   else 'b2.py not found or write failed'),
    },
    'middlewared.plugins.cloud_backup.restic': {
        'ok': restic_ok,
        'detail': ('patched on disk in overlay at boot' if restic_ok
                   else 'restic.py not found or write failed'),
    },
}
payload = {'patched_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()),
           'patches': patches}
sf = sys.argv[3]
tmp = sf + '.tmp'
try:
    with open(tmp, 'w') as f:
        json.dump(payload, f, indent=2)
    os.replace(tmp, sf)
    print('OK: Wrote hook_status.json')
except OSError as e:
    print(f'WARNING: Could not write hook_status.json: {e}')
" "$_b2_ok" "$_restic_ok" "$PATCH_DIR/hook_status.json" || true
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
