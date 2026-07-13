#!/bin/bash
# uninstall.sh — remove all traces of truecloud-patch from a TrueNAS box.

set -euo pipefail

VERSION="0.4.2"

PATCH_DIR="$(cd "$(dirname "$0")" && pwd)"
_HOOK_COMMENT='TrueCloud provider patch (S3/B2)'

echo "=== TrueNAS TrueCloud Provider Patch v${VERSION} — Uninstall ==="
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

# ── Unmount overlays ──────────────────────────────────────────────────────────

echo "Unmounting truecloud overlays (if any) ..."
_ov_found=0
for _tag in mw ui; do
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

# ── Revert file-level patches ─────────────────────────────────────────────────
# Unmounting the overlay is what normally reverts everything — the lower layer is
# the untouched /usr. But apply.sh only mounts an overlay when the directory is
# read-only; on a writable /usr it patches the real files in place. Uninstall
# would then remove the boot hook and report success while leaving every patch
# applied. Strip our appended blocks explicitly.

# Same implementation apply.sh uses (patch/mw_patch.py) — a second shell copy of
# this would be the untested one.
echo "Reverting any file-level patches ..."
python3 "$PATCH_DIR/patch/mw_patch.py" revert-all || \
    echo "  WARNING: could not revert file-level patches."
echo ""

# ── Unmount nested-snapshot staging trees ─────────────────────────────────────
# These bind mounts pin their ZFS snapshots, so they must go before anything
# tries to destroy those snapshots. Deepest first.

# Delegated to the patch module rather than reimplemented here: the depth
# ordering and lazy-umount fallback are fiddly, and a shell copy would be the
# untested one.
echo "Unmounting nested-snapshot staging trees (if any) ..."
if ! python3 "$PATCH_DIR/patch/truecloud_nested.py" cleanup; then
    echo "  WARNING: staging mounts remain. Unmount them manually; until you do,"
    echo "           the ZFS snapshots they pin cannot be destroyed."
fi

# The opt-in marker lives in the repo dir; remove it so a later re-install
# starts from the safe default (feature off).
if [ -f "$PATCH_DIR/nested_snapshots_enabled" ]; then
    rm -f "$PATCH_DIR/nested_snapshots_enabled"
    echo "  Removed nested-snapshot opt-in marker."
fi
echo ""

if [ "$_restore_failed" -eq 1 ]; then
    echo ""
    echo "ERROR: One or more UI bundle backups could not be restored." >&2
    echo "  $PATCH_DIR has been left intact (recover.sh and patch files are safe)." >&2
    echo "  Restore the backup(s) manually, then re-run uninstall.sh." >&2
    exit 1
fi

# Cancel a deferred boot restart if one is still queued — we restart ourselves.
systemctl stop truecloud-mw-restart.service 2>/dev/null || true
systemctl reset-failed truecloud-mw-restart.service 2>/dev/null || true

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
