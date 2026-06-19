#!/bin/bash
# recover.sh — emergency recovery if middlewared won't start after installing truecloud-patch.
#
# Run this from the TrueNAS shell (local console, SSH, or debug shell):
#
#   bash /mnt/tank/truenas-truecloud-patch/recover.sh
#
# What it does:
#   1. Creates a "disabled" file in the repo root — apply.sh checks for this
#      file at boot and skips all patching, so the next boot is always clean.
#   2. Unmounts any active truecloud overlays so the original /usr files are
#      visible immediately (no reboot required).
#   3. Restarts middlewared against the unpatched files.
#
# To re-enable the patch after investigating:
#   rm /mnt/tank/truenas-truecloud-patch/disabled
#   bash /mnt/tank/truenas-truecloud-patch/patch/apply.sh

VERSION="0.0.2"

PATCH_DIR="$(cd "$(dirname "$0")" && pwd)"

echo "=== TrueNAS TrueCloud Provider Patch v${VERSION} — Recover ==="
echo ""

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: must be run as root." >&2
    exit 1
fi

if [ ! -d "$PATCH_DIR" ]; then
    echo "ERROR: $PATCH_DIR not found — truecloud-patch may not be installed." >&2
    exit 1
fi

touch "$PATCH_DIR/disabled"
echo "Kill switch set: $PATCH_DIR/disabled created."

echo "Unmounting truecloud overlays ..."
_any=0
for _tag in mw ui; do
    if mount | grep -qF "truecloud-${_tag} on "; then
        _mnt=$(mount | grep "truecloud-${_tag} on " | awk '{print $3}' | head -1)
        if umount "$_mnt" 2>/dev/null; then
            echo "  Unmounted: $_mnt"
            _any=1
        else
            echo "  WARNING: Could not unmount $_mnt — a reboot will restore original files."
        fi
    fi
done
[ "$_any" -eq 0 ] && echo "  No overlays active."

echo "Restarting middlewared ..."
if systemctl restart middlewared; then
    echo ""
    echo "middlewared started successfully."
    echo "Your system is back to normal (Storj-only TrueCloud Backup)."
else
    echo ""
    echo "WARNING: middlewared did not start cleanly even with the patch disabled."
    echo "The problem is unrelated to truecloud-patch."
    echo "Check the system log for details:"
    echo "  journalctl -u middlewared -n 50"
    exit 1
fi
echo ""
echo "To re-enable the patch once you have investigated:"
echo "  rm $PATCH_DIR/disabled"
echo "  bash $PATCH_DIR/patch/apply.sh"
