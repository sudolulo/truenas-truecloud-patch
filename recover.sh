#!/bin/bash
# recover.sh — emergency recovery if middlewared won't start after installing truecloud-patch.
#
# Run this from the TrueNAS shell (local console, SSH, or debug shell):
#
#   bash /data/truecloud-patch/recover.sh
#
# What it does:
#   1. Creates /data/truecloud-patch/disabled  — sitecustomize.py checks for this
#      file at startup and skips the import hook entirely, so middlewared starts
#      clean without any of our code running.
#   2. Restarts middlewared.
#
# To re-enable the patch after investigating:
#   rm /data/truecloud-patch/disabled
#   bash /data/truecloud-patch/apply.sh

PATCH_DIR="/data/truecloud-patch"

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
systemctl restart middlewared
echo ""
echo "middlewared restarted without the truecloud-patch hook."
echo "Your system should be back to normal (Storj-only TrueCloud Backup)."
echo ""
echo "To re-enable the patch once you have investigated:"
echo "  rm $PATCH_DIR/disabled"
echo "  bash $PATCH_DIR/apply.sh"
