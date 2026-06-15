#!/bin/bash
# recover.sh — emergency recovery if middlewared won't start after installing truecloud-patch.
#
# Run this from the TrueNAS shell (local console, SSH, or debug shell):
#
#   bash /mnt/tank/truenas-truecloud-patch/recover.sh
#
# What it does:
#   1. Creates a "disabled" file in the repo root — sitecustomize.py checks for
#      this file at startup and skips the import hook entirely, so middlewared
#      starts clean without any of our code running.
#   2. Restarts middlewared.
#
# To re-enable the patch after investigating:
#   rm /mnt/tank/truenas-truecloud-patch/disabled
#   bash /mnt/tank/truenas-truecloud-patch/patch/apply.sh

PATCH_DIR="$(cd "$(dirname "$0")" && pwd)"

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
