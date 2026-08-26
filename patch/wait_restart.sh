#!/bin/bash
# patch/wait_restart.sh — payload of the transient `truecloud-mw-restart`
# unit that apply.sh schedules in boot context (Step 3).
#
# Why not restart middlewared directly from the unit: systemd ordering
# (`After=multi-user.target`, used up to v0.0.4) cannot see middlewared's
# *internal* boot work. When the boot targets are reached, two things are
# typically still in flight inside middlewared:
#
#   - ix-reporting.service's `midclt call reporting.start_service` (netdata,
#     which feeds the dashboard hardware stats), and
#   - the docker/apps startup task middlewared creates on its own
#     system-ready event (`docker.state.start_service`).
#
# Restarting middlewared while those run kills them, and nothing retries
# them until the next boot: every app stays down (`docker.status` FAILED),
# the dashboard shows no stats, and middleware-internal service state (e.g.
# the SMB backend) is left uninitialized. Observed on 25.10.4 with v0.0.4.
#
# So this script waits for both layers to settle before restarting. Every
# wait is bounded and fails open: worst case the restart still happens, just
# later — a restart on a settled system is harmless (docker, apps and
# netdata are independent processes; only the middleware API blips).
#
# NOTE: the unit must NOT be Type=oneshot. A oneshot's start job stays in
# the systemd job queue until the process exits, and `is-system-running
# --wait` below waits for that same queue to drain — the unit would deadlock
# on itself until the timeout. apply.sh schedules this with the default
# service type, whose start job completes at fork.
#
# THE RE-APPLY PASS (added 2026-08-26). Applying the patch at PREINIT and
# restarting later is only sound if the patched files are still on the live
# path at the moment middlewared re-imports them. They may not be: our patch
# lives in an overlay mounted *inside* /usr, and anything that remounts the
# hierarchy above it detaches or buries that overlay. Two things on a normal
# TrueNAS box do exactly that, both AFTER our PREINIT hook has run:
#
#   - `systemd-sysext merge/refresh` over /usr (an nvidia sysext, for
#     instance) — `Unmerged '/usr'` then `Merged extensions into '/usr'`;
#   - middlewared's own `docker.configure_nvidia`, which merges the stock
#     nvidia sysext over /usr when it brings docker up.
#
# PREINIT scripts run sequentially in id order, so a hook registered after
# ours always wins the race, silently. Observed 2026-08-19: our overlay was
# mounted at 16:41:56 and a sysext refresh tore /usr down four seconds later;
# the restart at 16:47:24 then loaded stock modules and every B2 cloud_backup
# job failed for the next nineteen hours while apply.log said "OK".
#
# Ordering the hooks cannot fix this — docker.configure_nvidia re-merges at
# runtime, long after every PREINIT hook is done. So instead of trusting the
# PREINIT pass, re-apply immediately before the restart (apply.sh is
# idempotent and re-mounts a lost overlay, keeping the same upperdir so
# already-patched files survive), verify the marker is really on the live
# path, and verify again afterwards.

PATCH_DIR="$(cd "$(dirname "$0")/.." && pwd)"
LOG="$PATCH_DIR/apply.log"

_log() { echo "[wait_restart] $*" >> "$LOG" 2>/dev/null; }

# Is the providers patch visible on the live filesystem path -- i.e. would a
# middlewared starting right now import it? Reads the marker apply.sh leaves
# in restic.py. Returns 0 when patched, 1 when stock, 2 when we cannot tell
# (no recorded middlewared dir yet, or the file is gone).
_patch_visible() {
    local mw_dir restic_py
    mw_dir=$(cat "$PATCH_DIR/.mw_dir" 2>/dev/null)
    [ -n "$mw_dir" ] || return 2
    restic_py="$mw_dir/plugins/cloud_backup/restic.py"
    [ -f "$restic_py" ] || return 2
    grep -q "TRUECLOUD_PATCH" "$restic_py" 2>/dev/null && return 0
    return 1
}

# 1. systemd layer: wait for the boot job queue to drain. This covers every
#    ix-* oneshot still activating, including ix-reporting's in-flight midclt
#    call. The exit code is irrelevant — a "degraded" boot (any unrelated
#    failed unit) is still a finished boot. The timeout only guards against
#    a boot that never settles (e.g. a unit stuck on a network wait).
timeout 900 systemctl is-system-running --wait > /dev/null 2>&1

# 2. middlewared layer: poll the docker state machine until it leaves the
#    transitional states (PENDING/INITIALIZING/STOPPING/MIGRATING — see
#    middlewared/plugins/docker/state_utils.py). An empty answer means
#    midclt could not respond at all; keep waiting. Cap at 10 minutes.
#    This also covers docker.configure_nvidia, the runtime /usr re-merge:
#    waiting for docker to reach a terminal state means the merge that would
#    bury our overlay has already happened by the time we re-apply below.
for _ in $(seq 1 120); do
    _status=$(midclt call docker.status 2>/dev/null \
                  | grep -oE '"status": "[A-Z_]+"' | cut -d'"' -f4)
    case "$_status" in
        RUNNING|STOPPED|UNCONFIGURED|FAILED|MIGRATION_FAILED) break ;;
    esac
    sleep 5
done

# 3. Grace period for middleware-internal ready-event tasks that expose no
#    queryable state (smb.configure and friends). Bounded insurance.
sleep 30

# 4. Re-apply pass. Boot has settled, so every sysext merge and docker nvidia
#    configuration that could bury our overlay is behind us. Re-running
#    apply.sh is cheap and idempotent: it re-mounts the overlay if it was
#    detached (same upperdir, so files patched at PREINIT reappear intact)
#    and re-patches anything that reverted to stock.
_patch_visible
case $? in
    0) _log "providers patch still visible on the live path before restart" ;;
    1) _log "PATCH LOST since PREINIT (something remounted /usr) — re-applying" ;;
    *) _log "cannot confirm patch state before restart — re-applying anyway" ;;
esac

TRUECLOUD_REAPPLY=1 /bin/bash "$PATCH_DIR/patch/apply.sh"

if ! _patch_visible; then
    _log "WARNING: patch is STILL not on the live path after the re-apply pass;"
    _log "WARNING: restarting anyway, but middlewared will load stock modules."
fi

# 5. The restart itself.
systemctl try-restart middlewared

# 6. Record what the restart landed on -- but do NOT restart again on a miss.
#
# `try-restart` returns as soon as middlewared is READY; it then brings docker
# up asynchronously, and `docker.configure_nvidia` merges the stock nvidia
# sysext over /usr at that point. That detaches our overlay AFTER the new
# middlewared has already imported the patched modules -- so a disk check here
# can report "missing" on a perfectly healthy system. Restarting on that signal
# would restart a correctly-patched middlewared and then hit the same race
# again, so the disk is deliberately not treated as a verdict after the restart.
#
# The authoritative answer is whether the running process holds the patch, and
# only middlewared can answer that. The alert source installed by apply.sh
# checks exactly that, in-process and hourly, and is what reports a genuine
# miss. What is still worth doing here is putting the overlay back, so the next
# middlewared restart -- whenever and whyever it happens -- finds patched files.
if _patch_visible; then
    _log "OK: providers patch present on the live path across the restart"
else
    _log "overlay detached again after the restart (expected when docker's"
    _log "nvidia sysext merge follows it) -- re-mounting for the next restart."
    _log "Whether THIS middlewared loaded the patch is answered in-process by"
    _log "the 'installed but NOT loaded' alert, not by this check."

    # Preserve hook_status.json's patched_at across this re-mount.
    #
    # create_task.py verify decides "loaded" by comparing middlewared's start
    # time against patched_at. This re-apply restores the SAME patch the boot
    # pass already applied, but it runs *after* the restart -- so letting it
    # re-stamp would make patched_at newer than the process that correctly
    # imported the patch, and verify would report FAIL forever, on every boot
    # where docker's sysext merge detaches the overlay. That is precisely the
    # lying-status failure this release exists to remove, so do not introduce a
    # new one. The snapshot lives in /run, never in the repo: a leftover file
    # there would leave the tree dirty and update.sh refuses to run over that.
    _saved_status=/run/truecloud-hook_status.pre
    cp -p "$PATCH_DIR/hook_status.json" "$_saved_status" 2>/dev/null

    TRUECLOUD_REAPPLY=1 /bin/bash "$PATCH_DIR/patch/apply.sh"

    if [ -f "$_saved_status" ]; then
        mv -f "$_saved_status" "$PATCH_DIR/hook_status.json" 2>/dev/null
    fi
fi

_log "=== deferred restart complete ==="
