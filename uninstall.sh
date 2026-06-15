#!/bin/bash
# uninstall.sh — removes all traces of the patch from a TrueNAS box.

set -euo pipefail

PATCH_DIR="/data/truecloud-patch"

echo "=== TrueNAS TrueCloud Provider Patch — Uninstall ==="
echo ""

if ! command -v midclt &>/dev/null; then
    echo "ERROR: midclt not found. Run this script on TrueNAS SCALE." >&2
    exit 1
fi

# ── Remove PREINIT registration ───────────────────────────────────────────────
IDS=$(midclt call initshutdownscript.query '[]' | \
    python3 -c "
import sys, json
for s in json.load(sys.stdin):
    if s.get('script') == '/data/truecloud-patch/apply.sh':
        print(s['id'])
" 2>/dev/null || true)

if [ -n "$IDS" ]; then
    for id in $IDS; do
        midclt call initshutdownscript.delete "$id" > /dev/null
        echo "Removed initshutdownscript id=$id"
    done
else
    echo "No initshutdownscript entry found (already removed or never installed)."
fi
echo ""

# ── Remove sitecustomize.py ───────────────────────────────────────────────────
SITE_PKG=$(python3 -c "import site; print(site.getsitepackages()[0])" 2>/dev/null || true)

if [ -n "$SITE_PKG" ] && [ -f "$SITE_PKG/sitecustomize.py" ]; then
    if grep -q "truecloud-patch" "$SITE_PKG/sitecustomize.py" 2>/dev/null; then
        rm "$SITE_PKG/sitecustomize.py"
        echo "Removed $SITE_PKG/sitecustomize.py"

        # Restore a pre-existing sitecustomize.py if we backed one up
        if [ -f "$SITE_PKG/sitecustomize.py.pre-truecloud-patch" ]; then
            mv "$SITE_PKG/sitecustomize.py.pre-truecloud-patch" \
               "$SITE_PKG/sitecustomize.py"
            echo "Restored previous sitecustomize.py"
        fi
    fi
fi
echo ""

# ── Restore UI bundle backup ──────────────────────────────────────────────────
RESTORED=0
for backup in $(find /usr/share/truenas /var/www/truenas -name "*.js.pre-truecloud-patch" 2>/dev/null); do
    original="${backup%.pre-truecloud-patch}"
    mv "$backup" "$original"
    echo "Restored: $original"
    RESTORED=1
done

if [ "$RESTORED" -eq 0 ]; then
    echo "No UI bundle backups found (patch will be undone by the next TrueNAS update)."
fi
echo ""

# ── Remove patch directory ────────────────────────────────────────────────────
if [ -d "$PATCH_DIR" ]; then
    rm -rf "$PATCH_DIR"
    echo "Removed $PATCH_DIR"
fi
echo ""

echo "Restarting middlewared ..."
systemctl restart middlewared

echo ""
echo "Uninstall complete. Refresh your browser to see the restored UI."
