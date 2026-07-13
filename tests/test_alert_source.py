"""Tests for the update-available alert source.

This file is imported by middlewared's `alert.load()`, which runs at STARTUP and
has **no try/except**:

    def load(self):
        for module in load_modules(.../alert/source):
            for cls in load_classes(module, AlertSource, (ThreadedAlertSource,)):
                source = cls(self.middleware)
                if source.name in ALERT_SOURCES:
                    raise RuntimeError(...)

So a module that raises on import takes middlewared's setup down with it. These
tests guard the realistic ways that could happen.
"""

import ast
import os
import re

import pytest

ALERT_SRC = os.path.join(os.path.dirname(__file__), "..", "patch", "alert_source.py")
APPLY_SH = os.path.join(os.path.dirname(__file__), "..", "patch", "apply.sh")


def source():
    with open(ALERT_SRC, encoding="utf-8") as fh:
        return fh.read()


def tree():
    return ast.parse(source())


class TestCannotBreakMiddlewaredAtImport:
    def test_it_compiles(self):
        compile(source(), "alert_source.py", "exec")

    def test_apply_sh_compiles_it_before_writing_it(self):
        # The substituted file is what middlewared imports. If it does not compile,
        # installing it would break startup — so apply.sh must refuse to write it.
        with open(APPLY_SH, encoding="utf-8") as fh:
            sh = fh.read()
        i = sh.index("alert_dst = os.path.join(mw_dir, 'alert', 'source'")
        block = sh[i:i + 1600]
        assert "compile(_body, alert_dst, 'exec')" in block
        assert block.index("compile(_body") < block.index("open(alert_dst, 'w'")

    def test_patch_dir_is_substituted_with_repr(self):
        # A directory containing a quote or backslash would otherwise produce a
        # syntax error in the installed module.
        with open(APPLY_SH, encoding="utf-8") as fh:
            sh = fh.read()
        assert "_body.replace('\"@PATCH_DIR@\"', repr(_patch_dir))" in sh

    @pytest.mark.parametrize("path", [
        "/mnt/tank/patch",
        '/mnt/we"ird/patch',      # a quote in the path
        "/mnt/back\\slash/patch",  # a backslash
    ])
    def test_substituted_module_compiles_for_awkward_paths(self, path):
        body = source().replace('"@PATCH_DIR@"', repr(path))
        compile(body, "alert_source.py", "exec")  # must not raise

    def test_no_io_at_module_import_time(self):
        # Anything at module scope runs during alert.load(). Only imports,
        # constants and class definitions are allowed.
        allowed = (ast.Import, ast.ImportFrom, ast.Assign, ast.AnnAssign,
                   ast.ClassDef, ast.FunctionDef, ast.Expr)
        for node in tree().body:
            assert isinstance(node, allowed), f"module-level {type(node).__name__}"
            if isinstance(node, ast.Expr):
                assert isinstance(node.value, ast.Constant), "only the docstring"


class TestAlertClassNaming:
    """middlewared's AlertClassMeta raises NameError unless the name ends in
    'AlertClass' — at import, inside alert.load(), which has no try/except."""

    def alert_classes(self):
        return [n for n in tree().body
                if isinstance(n, ast.ClassDef)
                and any(getattr(b, "id", "") == "AlertClass" for b in n.bases)]

    def test_there_are_alert_classes(self):
        assert self.alert_classes()

    def test_every_alert_class_name_ends_in_AlertClass(self):
        for cls in self.alert_classes():
            assert cls.name.endswith("AlertClass"), (
                f"{cls.name}: AlertClassMeta raises NameError on this"
            )

    def test_every_alert_class_defines_the_required_attrs(self):
        # category/level/title are NotImplemented on the base; a missing one shows
        # up as a broken alert rather than an error.
        for cls in self.alert_classes():
            names = {t.id for n in cls.body if isinstance(n, ast.Assign)
                     for t in n.targets if isinstance(t, ast.Name)}
            assert {"category", "level", "title", "text"} <= names, cls.name

    def test_alert_text_placeholders_match_the_args_we_pass(self):
        src = source()
        placeholders = set(re.findall(r"%\((\w+)\)s", src))
        # These are the keys built in _check().
        assert placeholders <= {"current", "latest", "summary", "dir"}


class TestNoSysPathMutation:
    def test_release_notes_is_loaded_by_path_not_sys_path(self):
        # sys.path.insert(0, ...) would shadow the stdlib for this interpreter, and
        # ThreadedAlertSource runs in a thread pool — mutating sys.path is a race.
        #
        # Check for actual MUTATION, not the string: the docstring legitimately
        # mentions sys.path to explain why it is avoided.
        src = source()
        assert "sys.path.insert" not in src
        assert "sys.path.append" not in src
        assert not re.search(r"^import sys$", src, re.M), "sys is not needed"
        assert "spec_from_file_location" in src


class TestNeverWritesToGit:
    def test_only_read_only_git_commands(self):
        # A `git fetch` from middlewared (running as root) would leave root-owned
        # objects in .git and break every later non-root git command — which is
        # exactly the breakage this project already hit once.
        src = source()
        for forbidden in ("fetch", "pull", "checkout", "clone", "reset"):
            assert f'"{forbidden}"' not in src, f"git {forbidden} writes to .git"
        assert '"ls-remote"' in src
