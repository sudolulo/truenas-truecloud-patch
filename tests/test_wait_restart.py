"""The deferred restart must re-apply the patch before it restarts middlewared.

Patching at PREINIT and restarting minutes later is only sound while the patched
files are still on the live path when middlewared re-imports them. They may not
be: the patch lives in an overlay mounted inside /usr, and anything that
remounts that hierarchy detaches it. On 2026-08-19 a systemd-sysext refresh over
/usr ran four seconds after apply.sh mounted its overlay; the deferred restart
then loaded stock modules and every B2 cloud_backup job failed for nineteen
hours while apply.log reported "OK".

These tests pin the ordering that makes that non-recoverable failure impossible:
re-apply, verify, restart, verify again.
"""

import os
import re
import subprocess

import pytest

HERE = os.path.dirname(__file__)
WAIT_RESTART = os.path.join(HERE, "..", "patch", "wait_restart.sh")
APPLY_SH = os.path.join(HERE, "..", "patch", "apply.sh")


def wait_restart_source():
    with open(WAIT_RESTART, encoding="utf-8") as fh:
        return fh.read()


def apply_source():
    with open(APPLY_SH, encoding="utf-8") as fh:
        return fh.read()


def test_wait_restart_is_executable():
    # apply.sh schedules it as `/bin/bash <script>`, but install.sh ships exec
    # bits and a mode-only diff once blocked update.sh outright (v0.6.0).
    assert os.access(WAIT_RESTART, os.X_OK)


def test_wait_restart_is_syntactically_valid():
    subprocess.run(["bash", "-n", WAIT_RESTART], check=True)


def test_reapply_runs_before_the_restart():
    src = wait_restart_source()
    reapply = src.index("TRUECLOUD_REAPPLY=1")
    restart = src.index("systemctl try-restart middlewared")
    assert reapply < restart, "the re-apply pass must precede the restart"


def test_restart_is_not_exec_so_verification_can_follow():
    # Up to v0.7.0 the script ended in `exec systemctl try-restart middlewared`,
    # which replaces the shell -- nothing could run afterwards. The post-restart
    # verification only exists if the restart is a plain call.
    src = wait_restart_source()
    assert not re.search(r"^\s*exec\s+systemctl", src, re.M)


def test_patch_is_verified_after_the_restart():
    src = wait_restart_source()
    restart = src.index("systemctl try-restart middlewared")
    assert "_patch_visible" in src[restart:], (
        "the script must check what the restart actually loaded"
    )


def test_verification_reads_the_marker_apply_sh_writes():
    # _patch_visible greps restic.py for TRUECLOUD_PATCH; apply.sh must still be
    # the thing that puts it there, or the check silently always fails.
    assert "TRUECLOUD_PATCH" in wait_restart_source()
    assert "TRUECLOUD_PATCH" in apply_source()


def test_verification_uses_the_recorded_middlewared_dir():
    # wait_restart.sh must not re-derive site-packages; apply.sh records it.
    assert ".mw_dir" in wait_restart_source()
    assert ".mw_dir" in apply_source()


def test_apply_sh_records_the_middlewared_dir():
    src = apply_source()
    assert re.search(r'>\s*"\$PATCH_DIR/\.mw_dir"', src), (
        "apply.sh must write the resolved middlewared dir for wait_restart.sh"
    )


def test_reapply_pass_does_not_schedule_another_restart():
    # wait_restart.sh owns the restart. If the re-apply pass scheduled its own
    # transient unit, each boot would spawn restarts recursively.
    src = apply_source()
    guard = src.index('if [ "${TRUECLOUD_REAPPLY:-0}" = "1" ]; then')
    systemd_run = src.index("systemd-run --no-block")
    assert guard < systemd_run, (
        "the TRUECLOUD_REAPPLY branch must short-circuit before systemd-run"
    )


def test_shadowed_overlay_is_remounted_not_accepted():
    """A buried overlay must never pass for a healthy one.

    _ensure_writable reaches its mount-table check only when the directory is
    NOT writable -- and a live overlay of ours is always writable. So a
    truecloud mount listed at that point is shadowed, and returning 0 there is
    exactly how a detached overlay used to masquerade as applied.
    """
    src = apply_source()
    start = src.index("_ensure_writable()")
    end = src.index("\n}", start)
    body = src[start:end]

    check = body.index('mount | grep -qF "truecloud-${tag} on ${dir} "')
    following = body[check:]
    # The old code did `return 0` immediately inside this branch.
    branch_end = following.index("fi")
    assert "return 0" not in following[:branch_end]
    assert "umount -l" in following[:branch_end]


def test_workdir_is_recreated_before_mounting():
    # overlayfs refuses a workdir left behind by a detached mount, so a stale
    # one would turn every re-mount attempt into "overlay mount failed".
    src = apply_source()
    start = src.index("_ensure_writable()")
    end = src.index("\n}", start)
    body = src[start:end]
    assert re.search(r'rm -rf "\$work"', body)


def test_upperdir_is_preserved_across_remounts():
    # The upperdir holds everything patched earlier this boot; reusing it is
    # what lets a re-mount restore those files instead of re-deriving them.
    src = apply_source()
    start = src.index("_ensure_writable()")
    end = src.index("\n}", start)
    body = src[start:end]
    assert 'rm -rf "$upper"' not in body


@pytest.mark.parametrize("state", ["0", "1", "2"])
def test_patch_visible_returns_three_distinct_states(state):
    # patched / stock / cannot-tell must stay distinguishable: "cannot tell"
    # has to re-apply rather than assume the patch is fine.
    src = wait_restart_source()
    assert f"return {state}" in src or f") return {state}" in src


def test_mount_retries_on_a_private_workdir():
    """A lazily-detached overlay can still pin the shared workdir.

    overlayfs refuses a workdir that is in use, so without a retry the re-mount
    this whole fix depends on would fail exactly when it is most needed.
    """
    src = apply_source()
    start = src.index("_ensure_writable()")
    end = src.index("\n}", start)
    body = src[start:end]
    assert body.count("mount -t overlay") == 2, "expected a retry mount"
    assert 'work="/run/truecloud-${tag}-work.$$"' in body
