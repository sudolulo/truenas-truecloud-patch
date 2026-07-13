"""Tests for create_task.py, focused on the restic repository password.

That password is the encryption key for the entire cloud backup repository. It
used to travel through `midclt call cloud_backup.create '<json>'` -- i.e. through
the subprocess's **argv**, which is world-readable via `ps` -- and `--password`
wrote it into the user's shell history forever.
"""

import importlib.util
import io
import os
import sys
import types

import pytest

SPEC = importlib.util.spec_from_file_location(
    "create_task",
    os.path.join(os.path.dirname(__file__), "..", "patch", "create_task.py"),
)


def load():
    mod = importlib.util.module_from_spec(SPEC)
    SPEC.loader.exec_module(mod)
    return mod


class Args:
    def __init__(self, password=None, password_stdin=False):
        self.password = password
        self.password_stdin = password_stdin


class TestPasswordNeverReachesArgv:
    """The whole reason this module talks to the client library."""

    def test_midclt_call_spawns_no_subprocess(self):
        import inspect

        src = inspect.getsource(load().midclt_call)
        assert "subprocess" not in src, (
            "shelling out to `midclt` puts cloud_backup.create's JSON -- including "
            "the restic repo password -- into argv, which any local user can read "
            "with ps"
        )
        assert "truenas_api_client" in src

    def test_errors_never_echo_the_call_arguments(self):
        # A failed cloud_backup.create must not print the body back at the user;
        # it contains the password.
        import inspect

        src = inspect.getsource(load().midclt_call)
        assert "{args}" not in src
        assert "args!r" not in src


class TestResolvePassword:
    def test_reads_from_stdin(self, monkeypatch):
        mod = load()
        monkeypatch.setattr(sys, "stdin", io.StringIO("s3cret\n"))
        assert mod._resolve_password(Args(password_stdin=True)) == "s3cret"

    def test_strips_only_the_trailing_newline(self, monkeypatch):
        # A password may legitimately contain spaces; only the line ending goes.
        mod = load()
        monkeypatch.setattr(sys, "stdin", io.StringIO("  pass word  \n"))
        assert mod._resolve_password(Args(password_stdin=True)) == "  pass word  "

    def test_cli_password_still_works_but_warns(self, monkeypatch, capsys):
        mod = load()
        pw = mod._resolve_password(Args(password="cli-secret"))
        assert pw == "cli-secret"
        assert "shell" in capsys.readouterr().err.lower(), "must warn about history"

    def test_prompts_when_neither_flag_given(self, monkeypatch):
        mod = load()
        monkeypatch.setattr(
            mod, "getpass", types.SimpleNamespace(getpass=lambda _p: "prompted")
        )
        assert mod._resolve_password(Args()) == "prompted"

    def test_rejects_both_flags(self, monkeypatch):
        mod = load()
        monkeypatch.setattr(sys, "stdin", io.StringIO("x\n"))
        with pytest.raises(SystemExit):
            mod._resolve_password(Args(password="a", password_stdin=True))

    def test_rejects_an_empty_password(self, monkeypatch):
        # An empty restic password would silently create an unencrypted-ish repo.
        mod = load()
        monkeypatch.setattr(sys, "stdin", io.StringIO("\n"))
        with pytest.raises(SystemExit):
            mod._resolve_password(Args(password_stdin=True))


class TestVersion:
    def test_version_is_not_stale(self):
        # __version__ sat at 0.2.0 through three releases because the drift check
        # only looked at VERSION= in shell scripts. It covers this file now.
        sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "tools"))
        from release_notes import normalise, script_versions

        repo = os.path.join(os.path.dirname(__file__), "..")
        versions = {normalise(v) for v in script_versions(repo).values()}
        assert len(versions) == 1, f"version drift: {sorted(versions)}"
