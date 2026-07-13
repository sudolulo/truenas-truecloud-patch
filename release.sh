#!/usr/bin/env bash
# Cut a release. Two stages, and you cannot skip the first one.
#
#   bash release.sh 0.6.0 --rc        stage 1: candidate. Invisible to users.
#   bash release.sh 0.6.0 --promote   stage 2: stable. Only if an rc passed HERE.
#
# WHY IT WORKS THIS WAY
#
# This repo once cut twelve releases in a day, several of them fixing the release
# before. Every one of those raises an update alert on every user's box. An alert
# people learn to ignore is worse than no alert, because one day it will be
# carrying a security fix.
#
# So: debugging happens across rc1, rc2, rc3 -- which update.sh and the alert
# source both filter out, so no user ever sees them -- and a stable tag is only
# reachable from a candidate that already went green on the identical commit.
# tools/release_gate.py enforces that here, and .github/workflows/release.yml
# enforces it again where it cannot be bypassed.
#
# Day to day you do not touch this script. You write your changes under
# `## Unreleased` in CHANGELOG.md and push to main. Releasing is a separate,
# deliberate act.

set -euo pipefail

# This file IS on every user's box -- update.sh clones the whole repo -- so it
# carries no VERSION= not because it is "not shipped", but because nothing reads
# it. VERSION= exists so the running system can say which patch it is; this script
# never runs on a running system. (Anything that DOES carry a VERSION= must be in
# release_notes.VERSIONED_FILES or it silently rots: create_task.py sat three
# releases behind for exactly that reason.)
#
# Running it on a user's box is a no-op by construction, and that is checked below
# rather than left to luck: update.sh pins the checkout to a tag in detached HEAD,
# and this refuses to run anywhere but an up-to-date `main` with push access.

cd "$(dirname "$(readlink -f "$0")")"

die() { printf '\033[31merror:\033[0m %s\n' "$*" >&2; exit 1; }
note() { printf '\033[36m==>\033[0m %s\n' "$*"; }
ok() { printf '\033[32m ok\033[0m %s\n' "$*"; }

usage() {
  sed -n '2,25p' "$0" | sed 's/^# \{0,1\}//'
  exit "${1:-0}"
}

# ── args ─────────────────────────────────────────────────────────────────────

target=""
mode=""
assume_yes=0

while [ $# -gt 0 ]; do
  case "$1" in
    --rc)       mode="rc" ;;
    --promote)  mode="promote" ;;
    --check)    mode="check" ;;
    -y|--yes)   assume_yes=1 ;;
    -h|--help)  usage 0 ;;
    -*)         die "unknown option: $1" ;;
    *)
      [ -n "$target" ] && die "give exactly one version"
      target="${1#v}"
      ;;
  esac
  shift
done

[ -n "$target" ] || usage 2
[ -n "$mode" ] || die "pick a stage: --rc (candidate) or --promote (stable)"

printf '%s' "$target" | grep -Eq '^[0-9]+\.[0-9]+\.[0-9]+$' \
  || die "version must be plain X.Y.Z (the -rcN suffix is added for you)"

# ── preflight ────────────────────────────────────────────────────────────────

[ -d .git ] || die "not a git checkout"

git diff --quiet && git diff --cached --quiet \
  || die "working tree is dirty. Commit or stash first -- a release must be
       reproducible from a commit, not from whatever happened to be on disk."

branch="$(git rev-parse --abbrev-ref HEAD)"
if [ "$branch" = "HEAD" ]; then
  # This is what an INSTALLED patch looks like: update.sh pins the checkout to a
  # release tag in detached HEAD. Someone has found this script in their clone and
  # run it. Say so plainly rather than emitting a confusing branch error.
  die "this is an installed checkout (detached at $(git describe --tags --always)),
       not a development one. release.sh is the maintainer tool that publishes new
       versions of the patch; it is not how you install or update one.

       To update this box:   bash update.sh"
fi
[ "$branch" = "main" ] || die "releases are cut from main, not '$branch'"

note "fetching tags"
git fetch --tags --quiet origin

if [ -n "$(git log --oneline "origin/$branch..$branch" 2>/dev/null)" ]; then
  die "local main has commits that are not pushed. Push first: the tag must point
       at a commit the world can actually fetch."
fi

# ── the gates: identical to the ones CI will run ─────────────────────────────

run_gates() {
  local tag="$1"
  note "gate: versions agree, CHANGELOG is complete"
  python3 tools/release_notes.py check "$tag" \
    || die "content gate failed (see above)"
  ok "content"

  note "gate: provenance"
  python3 tools/release_gate.py "$tag" -C . \
    || die "provenance gate failed (see above)"
  ok "provenance"
}

# ── tests, because a tag that fails its own tests is not a release ───────────

run_tests() {
  note "running the suite"
  python3 -m pytest tests -q || die "tests fail. Fix them; do not release around them."
  if command -v ruff >/dev/null 2>&1; then
    ruff check patch tests tools || die "lint fails"
  fi
  local f
  while IFS= read -r f; do
    bash -n "$f" || die "bash syntax error in $f"
  done < <(find . -name '*.sh' -not -path './.git/*')
  ok "suite"
}

confirm() {
  [ "$assume_yes" -eq 1 ] && return 0
  printf '\n%s [y/N] ' "$1"
  read -r reply </dev/tty
  case "$reply" in [yY]*) return 0 ;; *) die "aborted" ;; esac
}

# ── check ────────────────────────────────────────────────────────────────────

if [ "$mode" = "check" ]; then
  echo
  python3 - "$target" <<'PY'
import sys, os
sys.path.insert(0, os.path.join(os.getcwd(), "tools"))
from release_notes import unreleased_body
with open("CHANGELOG.md", encoding="utf-8") as fh:
    body = unreleased_body(fh.read())
if body:
    print("Unreleased, and would ship as v%s:\n" % sys.argv[1])
    print("\n".join("    " + line for line in body.splitlines()))
else:
    print("Nothing under `## Unreleased`. There is nothing to release.")
PY
  echo
  next_rc="$(python3 tools/release_gate.py "$target" --next-rc -C .)"
  echo "Next candidate would be: $next_rc"
  exit 0
fi

# ── stage 1: release candidate ───────────────────────────────────────────────

if [ "$mode" = "rc" ]; then
  tag="$(python3 tools/release_gate.py "$target" --next-rc -C .)"

  # The first candidate promotes `## Unreleased` and stamps the version into every
  # script. Later candidates (rc2+) are re-cuts of an already-stamped version, so
  # they only tag -- the CHANGELOG section for this version already exists, and
  # fixes found during rc go into it.
  if git rev-parse -q --verify "refs/tags/v$target-rc1" >/dev/null; then
    note "v$target is already stamped; cutting a follow-up candidate"
  else
    note "promoting '## Unreleased' -> v$target and stamping the scripts"
    python3 - "$target" <<'PY'
import datetime, os, re, sys
sys.path.insert(0, os.path.join(os.getcwd(), "tools"))
from release_notes import VERSIONED_FILES, promote

version = sys.argv[1]
today = datetime.date.today().isoformat()

with open("CHANGELOG.md", encoding="utf-8") as fh:
    text = fh.read()
try:
    out = promote(text, version, today)
except ValueError as e:
    sys.exit(f"error: {e}")
with open("CHANGELOG.md", "w", encoding="utf-8") as fh:
    fh.write(out)
print(f"    CHANGELOG.md  Unreleased -> v{version} - {today}")

for rel in VERSIONED_FILES:
    with open(rel, encoding="utf-8") as fh:
        src = fh.read()
    new, n = re.subn(
        r'^(VERSION=|__version__\s*=\s*)"[^"]+"',
        lambda m: f'{m.group(1)}"{version}"',
        src, count=1, flags=re.M,
    )
    if not n:
        sys.exit(f"error: {rel} has no VERSION= line to stamp")
    if new != src:
        with open(rel, "w", encoding="utf-8") as fh:
            fh.write(new)
        print(f"    {rel}  -> {version}")
PY
    git add -A
    git commit -q -m "release v$target"
    ok "stamped"
  fi

  # Gated as the rc tag it is: content is checked against the base version, and the
  # provenance gate is a no-op for candidates -- being one is the whole point.
  run_gates "$tag"
  run_tests

  echo
  note "about to cut $tag"
  echo "    commit:  $(git rev-parse --short HEAD)  $(git log -1 --format=%s)"
  echo
  echo "    A candidate is invisible to users: update.sh and the update alert both"
  echo "    ignore -rc tags. Install it on a real box, exercise it, and only then"
  echo "    run:  bash release.sh $target --promote"
  confirm "cut $tag?"

  git tag -a "$tag" -m "$tag"
  git push --quiet origin main
  git push --quiet origin "$tag"
  ok "pushed $tag"
  echo
  echo "CI is now testing $tag and publishing it as a PRE-RELEASE."
  echo "When you are satisfied:  bash release.sh $target --promote"
  exit 0
fi

# ── stage 2: promote to stable ───────────────────────────────────────────────

if [ "$mode" = "promote" ]; then
  tag="v$target"

  if git rev-parse -q --verify "refs/tags/$tag" >/dev/null; then
    next="$(echo "$target" | awk -F. '{printf "%d.%d.%d", $1, $2, $3+1}')"
    die "$tag already exists. A published version is immutable -- if it is broken,
       the fix ships as v$next, and it goes through a candidate like everything else."
  fi

  # The barrier. Fails unless an rc points at THIS commit.
  note "gate: was this exact commit a release candidate?"
  python3 tools/release_gate.py "$tag" -C . || {
    echo
    die "not promotable (see above)"
  }
  ok "provenance"

  run_gates "$tag"
  run_tests

  rcs="$(python3 - "$target" <<'PY'
import os, sys
sys.path.insert(0, os.path.join(os.getcwd(), "tools"))
from release_gate import rc_tags
print(", ".join(rc_tags(sys.argv[1])) or "none")
PY
)"

  echo
  note "about to publish $tag to every user"
  echo "    commit:      $(git rev-parse --short HEAD)"
  echo "    candidates:  $rcs"
  echo
  echo "    This raises an update alert on every installed box (unless the only"
  echo "    CHANGELOG section is Docs). Make sure it is worth interrupting people."
  confirm "publish $tag?"

  git tag -a "$tag" -m "$tag"
  git push --quiet origin "$tag"
  ok "pushed $tag"
  echo
  echo "CI is publishing the release. Users will be alerted within 24h."
  exit 0
fi
