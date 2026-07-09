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

exec systemctl try-restart middlewared
