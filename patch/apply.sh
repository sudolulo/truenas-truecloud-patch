#!/bin/bash
# /data/truecloud-patch/apply.sh
#
# PREINIT script registered via TrueNAS initshutdownscript.
# Runs on every boot before middlewared starts, re-applying patches that
# TrueNAS updates wipe from /usr/.
#
# Two things are patched:
#   1. sitecustomize.py  → monkey-patches B2 restic support + URL fix into
#                          middlewared at Python startup time (backend)
#   2. Angular JS bundle → adds S3 and B2 to the credential dropdown in
#                          the TrueCloud Backup task form (UI)

set -euo pipefail

PATCH_DIR="/data/truecloud-patch"
LOG="$PATCH_DIR/apply.log"

{
    echo "=== $(date -Iseconds) ==="

    # ── 1. Backend: install sitecustomize.py ─────────────────────────────
    SITE_PKG=$(python3 -c "import site; print(site.getsitepackages()[0])" 2>/dev/null || true)

    if [ -z "$SITE_PKG" ]; then
        echo "WARNING: could not determine site-packages path, skipping backend patch"
    else
        # If an unrelated sitecustomize.py exists, back it up once.
        if [ -f "$SITE_PKG/sitecustomize.py" ] && \
           ! grep -q "truecloud-patch" "$SITE_PKG/sitecustomize.py" 2>/dev/null; then
            cp "$SITE_PKG/sitecustomize.py" "$SITE_PKG/sitecustomize.py.pre-truecloud-patch"
            echo "Backed up existing sitecustomize.py"
        fi

        cp "$PATCH_DIR/sitecustomize.py" "$SITE_PKG/sitecustomize.py"
        echo "Installed sitecustomize.py → $SITE_PKG/sitecustomize.py"
    fi

    # ── 2. UI: patch Angular bundle ──────────────────────────────────────
    python3 "$PATCH_DIR/patch_ui.py"

    echo "=== done ==="

} >> "$LOG" 2>&1
