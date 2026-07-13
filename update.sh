#!/bin/bash
# update.sh — fetch a newer release of truecloud-patch and apply it.
#
# ── RUN THIS BY HAND. NEVER FROM CRON OR A SYSTEMD TIMER. ─────────────────────
#
# This patch injects Python into middlewared and re-applies itself at every boot.
# An unattended pull would let any bad upstream commit reach your box with no
# human in the loop, and take effect on the next reboot. That is not theoretical:
# v0.0.4 shipped a boot-time bug that took every app on the box down.
#
# The manual step IS the safety gate. Keep it.
#
# By default this updates to the newest RELEASE TAG, not to main. main can be
# mid-refactor; a tag is the tested artifact. Use --main only if you know why.
#
#   bash update.sh              # to the newest release, with a confirmation
#   bash update.sh --check      # show what would happen; change nothing
#   bash update.sh --rollback   # undo the last update

set -euo pipefail

VERSION="0.6.0"

PATCH_DIR="$(cd "$(dirname "$0")" && pwd)"
_PREV_FILE="$PATCH_DIR/.update_previous"

_target=""
_use_main=0
_assume_yes=0
_check_only=0
_rollback=0

usage() {
    cat <<USAGE
Usage: bash update.sh [options]

Options:
  --to <ref>    Update to a specific tag or commit (default: newest release tag)
  --main        Update to origin/main — UNRELEASED code, no guarantees
  --check       Show what an update would do and exit; changes nothing
  --rollback    Return to the revision recorded before the last update
  --yes, -y     Skip the confirmation prompt
  -h, --help    Show this help

Updating preserves your nested-snapshot opt-in setting either way.
USAGE
}

# An UNTRACKED file that the target tracks makes `git checkout` abort. The dirty-
# tree check deliberately ignores untracked files, so this slips past it and the
# checkout then dies mid-operation. Not hypothetical: a hand-copied
# patch/wait_restart.sh blocked a pull on a real box exactly this way.
#
# Used by BOTH the update and the rollback path -- rolling back moves the tree too,
# and would hit the identical failure.
_abort_if_untracked_blockers() {
    local ref="$1" blocking

    # Set intersection of {untracked, not ignored} and {tracked by the target}. Two
    # git calls, not one `ls-files --error-unmatch` per file in the target tree.
    # --exclude-standard is deliberate: git silently overwrites *ignored* files on
    # checkout, so those are not blockers — only untracked-and-not-ignored ones are.
    blocking="$(comm -12 \
        <(git ls-files --others --exclude-standard | sort) \
        <(git ls-tree -r --name-only "$ref" | sort) \
        | sed 's/^/    /')"

    [ -n "$blocking" ] || return 0

    echo "ERROR: these untracked files would be overwritten:" >&2
    printf '%s\n\n' "$blocking" >&2
    echo "  They exist here but git does not track them — most likely hand-copied" >&2
    echo "  or scp'd in. Move or delete them, then re-run." >&2

    # "Delete update.sh, then re-run update.sh" is impossible. If the script itself
    # is a blocker, it was hand-copied in to bootstrap; the honest answer is to
    # bootstrap with git instead, which installs it properly.
    case "$blocking" in
        *update.sh*)
            echo "" >&2
            echo "  update.sh itself is untracked here — you copied it in to bootstrap." >&2
            echo "  Do that with git instead, once; it installs update.sh properly:" >&2
            echo "" >&2
            echo "    rm -f $PATCH_DIR/update.sh" >&2
            echo "    git -C $PATCH_DIR checkout $ref" >&2
            echo "    bash $PATCH_DIR/install.sh" >&2
            echo "" >&2
            echo "  Every later update is then just: bash update.sh" >&2
            ;;
    esac
    exit 1
}

while [ $# -gt 0 ]; do
    case "$1" in
        --to)
            if [ -z "${2:-}" ]; then
                echo "ERROR: --to needs a tag, branch, or commit." >&2
                exit 1
            fi
            _target="$2"; shift ;;
        --main)     _use_main=1 ;;
        --check)    _check_only=1 ;;
        --rollback) _rollback=1 ;;
        --yes|-y)   _assume_yes=1 ;;
        -h|--help)  usage; exit 0 ;;
        *) echo "ERROR: unknown option: $1" >&2; echo "" >&2; usage >&2; exit 1 ;;
    esac
    shift
done

echo "=== TrueNAS TrueCloud Provider Patch — Update (v${VERSION}) ==="
echo ""

# ── Preflight ─────────────────────────────────────────────────────────────────

if [ "$(id -u)" -ne 0 ]; then
    echo "ERROR: must be run as root (install.sh needs it)." >&2
    exit 1
fi

cd "$PATCH_DIR"

if ! git rev-parse --git-dir >/dev/null 2>&1; then
    echo "ERROR: $PATCH_DIR is not a git clone — nothing to update." >&2
    echo "  Re-clone from https://git.onetick.ninja/flan/truenas-truecloud-patch" >&2
    exit 1
fi

# Past `sudo git pull`s can leave root-owned objects in .git that then break any
# non-root git command. We run as root, so we would only make that worse.
_owner="$(stat -c '%U' "$PATCH_DIR")"
if [ -n "$_owner" ] && [ "$_owner" != "root" ]; then
    chown -R "$_owner" "$PATCH_DIR/.git" 2>/dev/null || true
fi

# A dirty tree means someone edited or scp'd files in place; merging over that
# silently loses their changes, or conflicts halfway through.
if [ -n "$(git status --porcelain --untracked-files=no)" ]; then
    echo "ERROR: the working tree has uncommitted changes:" >&2
    git status --short --untracked-files=no >&2
    echo "" >&2
    echo "  Refusing to update over them. Commit, stash, or discard them first:" >&2
    echo "    git -C $PATCH_DIR checkout -- ." >&2
    exit 1
fi

# ── Rollback ──────────────────────────────────────────────────────────────────

if [ "$_rollback" -eq 1 ]; then
    if [ ! -f "$_PREV_FILE" ]; then
        echo "ERROR: no previous revision recorded — nothing to roll back to." >&2
        exit 1
    fi
    _prev="$(cat "$_PREV_FILE")"
    if ! git rev-parse --verify --quiet "${_prev}^{commit}" >/dev/null; then
        echo "ERROR: recorded revision '$_prev' is not a valid commit." >&2
        echo "  The history may have been rewritten. Pick a target explicitly:" >&2
        echo "    bash update.sh --to <tag>" >&2
        exit 1
    fi
    _abort_if_untracked_blockers "$_prev"
    echo "Rolling back to $_prev ..."
    git checkout -q --detach "$_prev"
    echo "Reverted. Re-applying ..."
    echo ""
    bash "$PATCH_DIR/install.sh"
    exit 0
fi

# ── Work out where we are and where we are going ──────────────────────────────

echo "Fetching ..."
git fetch --quiet --tags --prune origin

_current="$(git rev-parse HEAD)"
_current_desc="$(git describe --tags --always 2>/dev/null || echo "$_current")"

if [ -n "$_target" ]; then
    :
elif [ "$_use_main" -eq 1 ]; then
    _target="origin/main"
else
    # Newest release tag by VERSION order, not by tag date. Date order is only
    # correct while tags are created in ascending version order; it breaks the
    # moment a hotfix is tagged out of band (a v0.3.6 released after v0.4.0 would
    # sort as "newest" by date and silently downgrade the box).
    #
    # Filter to PLAIN vX.Y.Z: git's version sort ranks `v0.5.0-rc1` ABOVE `v0.5.0`
    # (verified), so without this a release candidate would be installed as though
    # it were the newest release. The release workflow deliberately supports
    # rc/beta/alpha tags, so they will exist.
    _target="$(git tag -l 'v*' --sort=-version:refname \
                 | grep -E '^v[0-9]+\.[0-9]+\.[0-9]+$' | head -1)"
    if [ -z "$_target" ]; then
        echo "ERROR: no release tags found; use --main to track unreleased code." >&2
        exit 1
    fi
fi

if ! _target_sha="$(git rev-parse --verify --quiet "${_target}^{commit}")"; then
    echo "ERROR: '$_target' is not a valid tag, branch, or commit." >&2
    exit 1
fi

echo "  current: $_current_desc"
echo "  target:  $_target ($(git rev-parse --short "$_target_sha"))"
echo ""

if [ "$_current" = "$_target_sha" ]; then
    echo "Already up to date. Nothing to do."
    exit 0
fi

_abort_if_untracked_blockers "$_target_sha"

# ── Show what is coming ───────────────────────────────────────────────────────

echo "Commits you do not have yet:"
git log --oneline --no-decorate "$_current..$_target_sha" | sed 's/^/  /' || true
echo ""

# Reuse tools/release_notes.py rather than re-implementing the extractor here —
# a second copy would be the untested one. Read the CHANGELOG *of the target*, so
# the notes describe what you are about to install.
if [ -f "$PATCH_DIR/tools/release_notes.py" ] && [ "$_use_main" -eq 0 ] \
       && [ -z "${_target##v*}" ]; then
    _cl="$(mktemp)"
    if git show "$_target_sha:CHANGELOG.md" > "$_cl" 2>/dev/null && [ -s "$_cl" ]; then
        echo "Release notes for $_target:"
        python3 "$PATCH_DIR/tools/release_notes.py" notes "$_target" "$_cl" \
            2>/dev/null | sed 's/^/  /' || echo "  (no notes for $_target)"
        echo ""
    fi
    rm -f "$_cl"
fi

if [ "$_use_main" -eq 1 ]; then
    echo "NOTE: --main tracks UNRELEASED code. It has passed CI, but it is not a"
    echo "      tested release, and apply.sh runs at every boot."
    echo ""
fi

if [ "$_check_only" -eq 1 ]; then
    echo "--check given; nothing changed."
    exit 0
fi

# ── Confirm ───────────────────────────────────────────────────────────────────

if [ "$_assume_yes" -eq 0 ]; then
    printf "Apply this update and restart middlewared? [y/N] "
    read -r _answer </dev/tty || _answer=""
    case "$_answer" in
        y|Y|yes|YES) ;;
        *) echo "Aborted. Nothing changed."; exit 0 ;;
    esac
    echo ""
fi

# ── Apply ─────────────────────────────────────────────────────────────────────

# Record where we were BEFORE moving, so --rollback works even if install.sh dies.
echo "$_current" > "$_PREV_FILE"

echo "Checking out $_target ..."
git checkout -q --detach "$_target_sha"
echo "  now at $(git describe --tags --always)"
echo ""

echo "Applying (this preserves your nested-snapshot setting) ..."
echo ""
if ! bash "$PATCH_DIR/install.sh"; then
    echo ""
    echo "ERROR: install.sh failed after updating." >&2
    echo "  Roll back with:  bash $PATCH_DIR/update.sh --rollback" >&2
    echo "  Or disable the patch entirely:  bash $PATCH_DIR/recover.sh" >&2
    exit 1
fi

echo ""
echo "=== Update complete ==="
echo "  $_current_desc  ->  $(git describe --tags --always)"
echo ""
if ! git symbolic-ref -q HEAD >/dev/null; then
    echo "NOTE: the checkout is now pinned to a release tag (detached HEAD), which is"
    echo "      what you want for a deployment. Plain \`git pull\` will not work here —"
    echo "      use \`bash update.sh\` from now on."
    echo ""
fi
echo "If anything looks wrong:"
echo "  bash $PATCH_DIR/update.sh --rollback    # back to $_current_desc"
echo "  bash $PATCH_DIR/recover.sh              # kill switch + restart"
