"""Behavioural tests for the "installed but NOT loaded" alert.

apply.log can only report what was written to disk. Whether the middlewared that
restarted afterwards actually imported those files is a different fact, and when
the two disagree nothing else notices: on 2026-08-19 every B2 backup failed for
nineteen hours while the log said OK. This alert is the only thing that closes
that gap, so it is tested against real objects rather than by reading source.

The middlewared package does not exist off-box, so the modules the alert source
imports are stubbed here.
"""

import importlib.util
import json
import os
import sys
import types

import pytest

ALERT_SRC = os.path.join(os.path.dirname(__file__), "..", "patch", "alert_source.py")


class _StubAlertClass:
    pass


class _StubThreadedAlertSource:
    pass


class _StubAlert:
    def __init__(self, klass, args=None, key=None):
        self.klass = klass
        self.args = args
        self.key = key


def _module(name):
    mod = types.ModuleType(name)
    sys.modules[name] = mod
    return mod


@pytest.fixture
def alert_source(monkeypatch, tmp_path):
    """Load patch/alert_source.py against stubbed middlewared modules."""
    for name in list(sys.modules):
        if name == "middlewared" or name.startswith("middlewared."):
            monkeypatch.delitem(sys.modules, name, raising=False)

    _module("middlewared")
    _module("middlewared.alert")
    base = _module("middlewared.alert.base")
    base.Alert = _StubAlert
    base.AlertClass = _StubAlertClass
    base.ThreadedAlertSource = _StubThreadedAlertSource
    base.AlertCategory = types.SimpleNamespace(SYSTEM="SYSTEM")
    base.AlertLevel = types.SimpleNamespace(
        INFO="INFO", WARNING="WARNING", CRITICAL="CRITICAL"
    )
    schedule = _module("middlewared.alert.schedule")
    schedule.IntervalSchedule = lambda delta: ("interval", delta)

    spec = importlib.util.spec_from_file_location("_tc_alert_source", ALERT_SRC)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    mod.PATCH_DIR = str(tmp_path)
    return mod


def _write_status(tmp_path, providers_active=True):
    payload = {
        "patched_at": "2026-08-26T00:00:00Z",
        "patches": {
            "providers": {"ok": True, "active": providers_active, "detail": "x"},
            "nested_snapshots": {"ok": True, "active": True, "detail": "x"},
        },
    }
    (tmp_path / "hook_status.json").write_text(json.dumps(payload))


def _install_provider_modules(monkeypatch, *, restic_patched, b2_patched):
    """Stub the two modules the alert inspects, in the requested state."""
    plugins = _module("middlewared.plugins")
    _module("middlewared.plugins.cloud_backup")
    restic = _module("middlewared.plugins.cloud_backup.restic")

    def get_restic_config(task):
        return None

    if restic_patched:
        get_restic_config._truecloud_patched = True
    restic.get_restic_config = get_restic_config

    rclone_base = _module("middlewared.rclone.base")
    _module("middlewared.rclone")
    _module("middlewared.rclone.remote")
    b2_mod = _module("middlewared.rclone.remote.b2")

    class BaseRcloneRemote:
        def get_restic_config(self, task):
            raise NotImplementedError

    class B2RcloneRemote(BaseRcloneRemote):
        pass

    if b2_patched:
        B2RcloneRemote.get_restic_config = staticmethod(lambda task: ("url", {}))

    rclone_base.BaseRcloneRemote = BaseRcloneRemote
    b2_mod.B2RcloneRemote = B2RcloneRemote
    b2_mod.BaseRcloneRemote = BaseRcloneRemote
    plugins.__path__ = []

    for name in (
        "middlewared.plugins",
        "middlewared.plugins.cloud_backup",
        "middlewared.plugins.cloud_backup.restic",
        "middlewared.rclone",
        "middlewared.rclone.base",
        "middlewared.rclone.remote",
        "middlewared.rclone.remote.b2",
    ):
        monkeypatch.setitem(sys.modules, name, sys.modules[name])


def _source(alert_source):
    cls = alert_source.TrueCloudPatchNotLoadedAlertSource
    return cls.__new__(cls)


def test_no_alert_when_patch_is_loaded(alert_source, monkeypatch, tmp_path):
    _write_status(tmp_path)
    _install_provider_modules(monkeypatch, restic_patched=True, b2_patched=True)
    assert _source(alert_source)._check() is None


def test_alert_when_middlewared_loaded_stock_modules(alert_source, monkeypatch, tmp_path):
    """The exact 2026-08-19 state: patched on disk, stock in the process."""
    _write_status(tmp_path)
    _install_provider_modules(monkeypatch, restic_patched=False, b2_patched=False)
    alert = _source(alert_source)._check()
    assert alert is not None
    assert alert.klass is alert_source.TrueCloudPatchNotLoadedAlertClass


def test_alert_when_only_b2_half_is_missing(alert_source, monkeypatch, tmp_path):
    # b2.py is the half that supplies B2's get_restic_config. restic.py alone
    # being patched still means every B2 task raises NotImplementedError.
    _write_status(tmp_path)
    _install_provider_modules(monkeypatch, restic_patched=True, b2_patched=False)
    assert _source(alert_source)._check() is not None


def test_alert_when_only_restic_half_is_missing(alert_source, monkeypatch, tmp_path):
    _write_status(tmp_path)
    _install_provider_modules(monkeypatch, restic_patched=False, b2_patched=True)
    assert _source(alert_source)._check() is not None


def test_silent_when_the_kill_switch_is_set(alert_source, monkeypatch, tmp_path):
    # The operator turned the patch off on purpose; stock is the intended state.
    _write_status(tmp_path)
    (tmp_path / "disabled").write_text("")
    _install_provider_modules(monkeypatch, restic_patched=False, b2_patched=False)
    assert _source(alert_source)._check() is None


def test_silent_when_providers_module_is_retired(alert_source, monkeypatch, tmp_path):
    # TrueNAS went native for B2: not loading our providers patch is correct.
    _write_status(tmp_path, providers_active=False)
    _install_provider_modules(monkeypatch, restic_patched=False, b2_patched=False)
    assert _source(alert_source)._check() is None


def test_silent_when_the_patch_was_never_applied_here(alert_source, monkeypatch, tmp_path):
    # No hook_status.json at all -- nothing claims a patch, so nothing is broken.
    _install_provider_modules(monkeypatch, restic_patched=False, b2_patched=False)
    assert _source(alert_source)._check() is None


def test_update_alert_silencer_does_not_mute_a_broken_backup_path(
    alert_source, monkeypatch, tmp_path
):
    # update_alerts_disabled mutes release notifications. It must not hide the
    # fact that TrueCloud backups are silently running stock.
    _write_status(tmp_path)
    (tmp_path / "update_alerts_disabled").write_text("")
    _install_provider_modules(monkeypatch, restic_patched=False, b2_patched=False)
    assert _source(alert_source)._check() is not None


def test_check_sync_never_raises(alert_source, monkeypatch, tmp_path):
    """An alert source that raises is polled forever inside middlewared."""
    _write_status(tmp_path)

    def boom(self):
        raise RuntimeError("provider import exploded")

    monkeypatch.setattr(
        alert_source.TrueCloudPatchNotLoadedAlertSource, "_check", boom, raising=True
    )
    assert _source(alert_source).check_sync() is None


def test_alert_is_critical_and_names_the_recovery_command(alert_source):
    klass = alert_source.TrueCloudPatchNotLoadedAlertClass
    assert klass.level == "CRITICAL"
    assert "install.sh" in klass.text
