#!/bin/bash
# /data/truecloud-patch/apply.sh
#
# Registered as a TrueNAS PREINIT initshutdownscript.
# Runs on every boot BEFORE middlewared starts, so patches land before
# the first Python process for middlewared is created.
#
# TrueNAS updates replace /usr/ entirely; this script re-applies two patches:
#
#   1. sitecustomize.py  — Python executes this automatically at startup.
#                          Monkey-patches B2 restic support and fixes the
#                          URL builder for empty-host providers.
#
#   2. Angular JS bundle — Widens the TrueCloud Backup credential dropdown
#                          from Storj-only to include S3 and B2.
#
# Design principle: every step is independently fail-safe.
# A failed patch logs a warning and continues; middlewared always starts.
# Never use `set -e` in a PREINIT script.

PATCH_DIR="/data/truecloud-patch"
LOG="$PATCH_DIR/apply.log"

# Rotate log at 512 KB to avoid unbounded growth on a system volume.
if [ -f "$LOG" ] && [ "$(wc -c < "$LOG")" -gt 524288 ]; then
    mv "$LOG" "${LOG}.1"
fi

exec >> "$LOG" 2>&1
echo "=== $(date -Iseconds) ==="

# Kill switch: if this file exists, skip all patching and exit cleanly.
# Recovery: touch /data/truecloud-patch/disabled (then reboot or restart middlewared).
if [ -f "$PATCH_DIR/disabled" ]; then
    echo "Kill switch active ($PATCH_DIR/disabled exists) — patch not applied."
    echo "To re-enable: rm $PATCH_DIR/disabled"
    echo "=== done ==="
    exit 0
fi

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
    fi

    echo "$py"
}

# ── Step 1: sitecustomize.py ──────────────────────────────────────────────────

echo "--- backend patch ---"

PYTHON=$(find_mw_python)
echo "Using Python: $PYTHON"

SITE_PKG=$("$PYTHON" -c "import site; print(site.getsitepackages()[0])" 2>/dev/null || true)

if [ -z "$SITE_PKG" ]; then
    echo "WARNING: Cannot determine site-packages directory; skipping backend patch."
    echo "  Run: $PYTHON -c \"import site; print(site.getsitepackages())\""
else
    # Back up any pre-existing sitecustomize.py that isn't ours.
    if [ -f "$SITE_PKG/sitecustomize.py" ] && \
       ! grep -q "truecloud-patch" "$SITE_PKG/sitecustomize.py" 2>/dev/null; then
        cp "$SITE_PKG/sitecustomize.py" \
           "$SITE_PKG/sitecustomize.py.pre-truecloud-patch"
        echo "OK: Backed up existing sitecustomize.py"
    fi

    if cp "$PATCH_DIR/sitecustomize.py" "$SITE_PKG/sitecustomize.py" 2>/dev/null; then
        echo "OK: Installed sitecustomize.py → $SITE_PKG/sitecustomize.py"
    else
        echo "WARNING: Failed to write $SITE_PKG/sitecustomize.py (permission error?)"
    fi
fi

# ── Step 2: Angular bundle ────────────────────────────────────────────────────

echo "--- UI patch ---"

"$PYTHON" "$PATCH_DIR/patch_ui.py" || echo "WARNING: patch_ui.py exited non-zero; UI dropdown may still show Storj only."

# ── Done ──────────────────────────────────────────────────────────────────────

echo "=== done ==="
