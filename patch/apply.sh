#!/bin/bash
# patch/apply.sh — registered as a TrueNAS PREINIT initshutdownscript.
#
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
    # Already our overlay from an earlier run this boot?
    if mount | grep -qF "truecloud-${tag} on "; then
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

# ── Step 1: sitecustomize.py ──────────────────────────────────────────────────

echo "--- backend patch ---"

PYTHON=$(find_mw_python)
echo "Using Python: $PYTHON"

SITE_PKG=$("$PYTHON" -c "import site; print(site.getsitepackages()[0])" 2>/dev/null || true)

if [ -z "$SITE_PKG" ]; then
    echo "WARNING: Cannot determine site-packages directory; skipping backend patch."
    echo "  Run: $PYTHON -c \"import site; print(site.getsitepackages())\""
else
    _can_install=true
    # On immutable OS, ensure site-packages is writable via overlay before
    # attempting any writes (backup, tmp file, mv).
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
        # Pass PATCH_DIR via env var so arbitrary path characters don't break
        # the substitution (sed metacharacters & and | are unsafe in shell-
        # interpolated replacement strings).  Write to a temp file first so a
        # failed substitution never truncates the existing sitecustomize.py.
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
