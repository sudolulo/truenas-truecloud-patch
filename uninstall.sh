#!/bin/bash
# uninstall.sh — remove all traces of truecloud-patch from a TrueNAS box.

set -euo pipefail

PATCH_DIR="$(cd "$(dirname "$0")" && pwd)"
_HOOK_COMMENT='TrueCloud provider patch (S3/B2)'

echo "=== TrueNAS TrueCloud Provider Patch — Uninstall ==="
echo ""

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: must be run as root." >&2
    exit 1
fi

if ! command -v midclt &>/dev/null; then
    echo "ERROR: midclt not found. Run this script on TrueNAS SCALE." >&2
    exit 1
fi

# ── Remove PREINIT hook ───────────────────────────────────────────────────────

echo "Removing PREINIT boot hook ..."

IDS=$(midclt call initshutdownscript.query '[]' | \
    python3 -c "
import sys, json
for s in json.load(sys.stdin):
    if s.get('comment') == '$_HOOK_COMMENT':
        print(s['id'])
" 2>/dev/null || true)

if [ -n "$IDS" ]; then
    for id in $IDS; do
        if midclt call initshutdownscript.delete "$id" > /dev/null; then
            echo "  Removed initshutdownscript id=$id"
        else
            echo "  WARNING: could not delete id=$id (already gone?)"
        fi
    done
else
    echo "  No entry found (already removed or never installed)."
fi
echo ""

# ── Remove sitecustomize.py ───────────────────────────────────────────────────

echo "Removing sitecustomize.py ..."

# Use the same Python detection logic as apply.sh
PYTHON="python3"
if [ -x /usr/bin/middlewared ]; then
    shebang=$(dd if=/usr/bin/middlewared bs=256 count=1 2>/dev/null | head -1 || true)
    if [[ "$shebang" =~ ^'#!'(/[^[:space:]]+python[^[:space:]]*) ]]; then
        PYTHON="${BASH_REMATCH[1]}"
    elif [[ "$shebang" =~ ^'#!/usr/bin/env '(python[^[:space:]]*) ]]; then
        PYTHON=$(command -v "${BASH_REMATCH[1]}" 2>/dev/null || echo "python3")
    fi
fi

if ! "$PYTHON" -c "import middlewared" 2>/dev/null; then
    echo "  WARNING: '$PYTHON' cannot import middlewared; falling back to python3"
    PYTHON="python3"
    if ! "$PYTHON" -c "import middlewared" 2>/dev/null; then
        echo "  WARNING: 'python3' also cannot import middlewared; sitecustomize.py may be removed from the wrong location."
    fi
fi

SITE_PKG=$("$PYTHON" -c "
import os
try:
    import middlewared
    print(os.path.dirname(os.path.dirname(os.path.abspath(middlewared.__file__))))
except ImportError:
    import site
    print(site.getsitepackages()[0])
" 2>/dev/null || true)

if [ -n "$SITE_PKG" ] && [ -f "$SITE_PKG/sitecustomize.py" ]; then
    if grep -q "truecloud-patch" "$SITE_PKG/sitecustomize.py" 2>/dev/null; then
        if [ -f "$SITE_PKG/sitecustomize.py.pre-truecloud-patch" ]; then
            # mv atomically overwrites our file with the vendor original —
            # safer than rm-then-mv if /usr is transiently read-only.
            mv "$SITE_PKG/sitecustomize.py.pre-truecloud-patch" \
               "$SITE_PKG/sitecustomize.py"
            echo "  Restored previous sitecustomize.py"
        else
            rm "$SITE_PKG/sitecustomize.py"
            echo "  Removed $SITE_PKG/sitecustomize.py"
        fi
    else
        echo "  $SITE_PKG/sitecustomize.py is not ours; leaving it alone."
    fi
else
    echo "  Not found (already removed or install didn't place it here)."
fi

# Handle orphaned backup when sitecustomize.py was removed (e.g. by a TrueNAS
# update) but the .pre-truecloud-patch file survived in the same directory.
if [ -n "$SITE_PKG" ] && [ ! -f "$SITE_PKG/sitecustomize.py" ] && \
   [ -f "$SITE_PKG/sitecustomize.py.pre-truecloud-patch" ]; then
    mv "$SITE_PKG/sitecustomize.py.pre-truecloud-patch" \
       "$SITE_PKG/sitecustomize.py"
    echo "  Restored orphaned sitecustomize.py backup"
fi
echo ""

# ── Restore UI bundle ─────────────────────────────────────────────────────────

echo "Restoring UI bundle backup ..."

RESTORED=0
_restore_failed=0
while IFS= read -r backup; do
    original="${backup%.pre-truecloud-patch}"
    if mv "$backup" "$original"; then
        echo "  Restored: $original"
        RESTORED=1
    else
        echo "  WARNING: Could not restore $original — backup left at $backup"
        _restore_failed=1
    fi
# Keep these paths in sync with WEBUI_CANDIDATES in patch/patch_ui.py
done < <(find /usr/share/truenas /usr/share/truenas-ui /var/www/truenas \
              -name "*.js.pre-truecloud-patch" 2>/dev/null)

if [ "$RESTORED" -eq 0 ]; then
    echo "  No backup files found."
    echo "  On an immutable OS the UI patch is volatile and already gone after reboot."
fi
echo ""

# ── Unmount overlays (TrueNAS 25.x immutable OS) ─────────────────────────────

echo "Unmounting truecloud overlays (if any) ..."
_ov_found=0
for _tag in sc ui; do
    if mount | grep -qF "truecloud-${_tag} on "; then
        _ov_mnt=$(mount | grep "truecloud-${_tag} on " | awk '{print $3}' | head -1)
        if umount "$_ov_mnt" 2>/dev/null; then
            echo "  Unmounted: $_ov_mnt"
        else
            echo "  WARNING: Could not unmount overlay on $_ov_mnt"
        fi
        _ov_found=1
    fi
done
if [ "$_ov_found" -eq 0 ]; then
    echo "  None active."
fi
echo ""

if [ "$_restore_failed" -eq 1 ]; then
    echo ""
    echo "ERROR: One or more UI bundle backups could not be restored." >&2
    echo "  $PATCH_DIR has been left intact (recover.sh and patch files are safe)." >&2
    echo "  Restore the backup(s) manually, then re-run uninstall.sh." >&2
    exit 1
fi

# ── Remove patch directory ────────────────────────────────────────────────────

if [ -d "$PATCH_DIR" ]; then
    rm -rf "$PATCH_DIR"
    echo "Removed $PATCH_DIR"
fi
echo ""

echo "Restarting middlewared ..."
if systemctl restart middlewared; then
    echo ""
    echo "Uninstall complete. Refresh your browser to see the restored UI."
else
    echo ""
    echo "WARNING: middlewared did not start cleanly after uninstall."
    echo "Check the system log for details:"
    echo "  journalctl -u middlewared -n 50"
    exit 1
fi
