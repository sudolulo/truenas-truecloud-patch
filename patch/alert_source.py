"""TrueNAS alert: a truecloud-patch update is available.

Installed by patch/apply.sh into middlewared/alert/source/, where middlewared
discovers and polls it natively — no cron job, no systemd timer.

@PATCH_DIR@ is substituted at install time.

Two rules govern this file:

1. **It must never break middlewared.** It runs inside the alert framework on a
   timer. Every failure path returns None (no alert) rather than raising.

2. **It must not nag.** A release whose CHANGELOG only has a "### Docs" section
   changed no code, and nobody wants an alert because a README was reworded. The
   CHANGELOG's own section headings are the signal — see tools/release_notes.py.

It also never writes to the repository. `git ls-remote` is read-only and the
CHANGELOG is fetched over HTTPS, so this cannot leave root-owned objects in .git
the way a `git fetch` from middlewared (running as root) would.
"""

import datetime
import importlib.util
import logging
import os
import re
import subprocess
import urllib.request

from middlewared.alert.base import (
    Alert,
    AlertCategory,
    AlertClass,
    AlertLevel,
    ThreadedAlertSource,
)
from middlewared.alert.schedule import IntervalSchedule

logger = logging.getLogger(__name__)

PATCH_DIR = "@PATCH_DIR@"
DISABLED_MARKER = os.path.join(PATCH_DIR, "update_alerts_disabled")

_TAG_RE = re.compile(r"^v\d+\.\d+\.\d+$")
_VERSION_RE = re.compile(r'^VERSION="([^"]+)"', re.M)

#: owner/repo out of any of:
#:   git@github.com:sudolulo/repo.git
#:   https://github.com/sudolulo/repo.git
#:   ssh://git@git.onetick.ninja:55214/flan/repo.git
#:   https://git.onetick.ninja/flan/repo.git
#: The SSH port is deliberately not captured: it is not the web port.
_REMOTE_RE = re.compile(
    r"^(?:\w+://)?(?:[^@/]+@)?([^:/]+)(?::\d+)?[:/]([^/]+)/([^/]+?)(?:\.git)?/?$"
)

_TIMEOUT = 20


class TrueCloudPatchUpdateAlertClass(AlertClass):
    category = AlertCategory.SYSTEM
    level = AlertLevel.INFO
    title = "truecloud-patch update available"
    text = (
        "truecloud-patch %(current)s is installed; %(latest)s is available.%(summary)s "
        "Update with:  bash %(dir)s/update.sh"
    )


class TrueCloudPatchSecurityUpdateAlertClass(AlertClass):
    category = AlertCategory.SYSTEM
    level = AlertLevel.WARNING
    title = "truecloud-patch security update available"
    text = (
        "truecloud-patch %(current)s is installed; %(latest)s contains a SECURITY "
        "fix.%(summary)s Update with:  bash %(dir)s/update.sh"
    )


class TrueCloudPatchUpdateAlertSource(ThreadedAlertSource):
    schedule = IntervalSchedule(datetime.timedelta(hours=24))
    run_on_backup_node = False

    def check_sync(self):
        try:
            return self._check()
        except Exception:
            # An alert source must never take middlewared down with it.
            logger.debug("truecloud-patch update check failed", exc_info=True)
            return None

    # ── internals ────────────────────────────────────────────────────────────

    def _git(self, *args):
        # List form, never shell=True, and every `args` value is a literal from
        # this file -- nothing user-supplied reaches the command line. The partial
        # `git` path is moot: this runs as root inside middlewared, so anyone who
        # can poison PATH already has root.
        return subprocess.run(  # noqa: S603
            ["git", "-C", PATCH_DIR, *args],  # noqa: S607
            capture_output=True, text=True, timeout=_TIMEOUT, check=True,
        ).stdout

    def _check(self):
        if os.path.exists(DISABLED_MARKER):
            return None
        if not os.path.isdir(os.path.join(PATCH_DIR, ".git")):
            return None

        current = self._installed_version()
        if not current:
            return None

        latest = self._latest_release_tag()
        if not latest:
            return None

        rn = self._release_notes()
        if rn is None:
            return None
        significance, version_tuple = rn.significance, rn.version_tuple

        if version_tuple(latest) <= version_tuple(current):
            return None

        level, versions, summary = self._classify(
            current, latest, significance
        )

        # Documentation-only releases are not worth an alert. This is the whole
        # point: nobody should get a notification because a README was reworded.
        if level == "docs":
            logger.debug(
                "truecloud-patch %s -> %s is documentation-only; not alerting",
                current, latest,
            )
            return None

        args = {
            "current": f"v{current}",
            "latest": latest,
            "summary": summary,
            "dir": PATCH_DIR,
        }
        klass = (
            TrueCloudPatchSecurityUpdateAlertClass if level == "security"
            else TrueCloudPatchUpdateAlertClass
        )
        return Alert(klass, args, key=[current, latest])

    def _release_notes(self):
        """Load tools/release_notes.py by path.

        NOT via sys.path: prepending would shadow the stdlib for this interpreter,
        and this runs in middlewared's thread pool, so mutating sys.path is a race.
        """
        path = os.path.join(PATCH_DIR, "tools", "release_notes.py")
        try:
            spec = importlib.util.spec_from_file_location("_tc_release_notes", path)
            mod = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(mod)
            return mod
        except Exception:
            logger.debug("could not load release_notes", exc_info=True)
            return None

    def _installed_version(self):
        """The version of the patch actually checked out here."""
        try:
            with open(os.path.join(PATCH_DIR, "patch", "apply.sh"), encoding="utf-8") as fh:
                m = _VERSION_RE.search(fh.read())
        except OSError:
            return None
        return m.group(1) if m else None

    def _latest_release_tag(self):
        """Newest plain vX.Y.Z tag on the remote. Read-only: no .git writes.

        Pre-release tags (-rc, -beta) are excluded: git's version sort ranks
        v0.5.0-rc1 above v0.5.0, so including them would advertise a release
        candidate as the latest stable.
        """
        try:
            out = self._git("ls-remote", "--tags", "--refs", "origin")
        except Exception:
            return None

        tags = []
        for line in out.splitlines():
            parts = line.split("refs/tags/")
            if len(parts) == 2 and _TAG_RE.match(parts[1].strip()):
                tags.append(parts[1].strip())
        if not tags:
            return None

        return max(tags, key=lambda t: tuple(int(x) for x in t.lstrip("v").split(".")))

    def _classify(self, current, latest, significance):
        """(level, versions, one-line summary). Falls back to alerting."""
        text = self._remote_changelog(latest)
        if text is None:
            # Cannot tell whether it matters. Alert rather than risk hiding a
            # security fix -- but say that we could not tell.
            return "notable", [], " (could not read the changelog)"

        level, versions, headings = significance(text, current, latest)
        if level == "docs":
            return level, versions, ""

        seen, ordered = set(), []
        for h in headings:
            if h not in seen:
                seen.add(h)
                ordered.append(h.capitalize())
        detail = ", ".join(ordered)
        return level, versions, f" Changes: {detail}." if detail else ""

    def _changelog_url(self, tag):
        """Where to read CHANGELOG.md at `tag`, derived from the origin remote.

        Forge-agnostic on purpose. This project is canonically hosted on Gitea and
        mirrored to GitHub, and hard-coding either one has a nastier failure than it
        looks: when the changelog cannot be read, _classify() falls back to
        "notable" and alerts ANYWAY, because the alternative is silently hiding a
        security fix. So a stale URL does not disable the alert -- it makes the
        alert fire on every release including documentation-only ones, which is
        precisely the nagging this whole mechanism exists to prevent.
        """
        try:
            remote = self._git("remote", "get-url", "origin").strip()
        except Exception:
            return None

        m = _REMOTE_RE.match(remote)
        if not m:
            return None

        host, owner, repo = m.group(1), m.group(2), m.group(3)

        if host.endswith("github.com"):
            return f"https://raw.githubusercontent.com/{owner}/{repo}/{tag}/CHANGELOG.md"

        # Gitea and Forgejo both serve /{owner}/{repo}/raw/tag/{tag}/{path} over the
        # web port, which is not the SSH port the remote may name.
        return f"https://{host}/{owner}/{repo}/raw/tag/{tag}/CHANGELOG.md"

    def _remote_changelog(self, tag):
        """CHANGELOG.md at `tag`, over HTTPS. None if it cannot be read."""
        url = self._changelog_url(tag)
        if not url:
            return None
        try:
            with urllib.request.urlopen(url, timeout=_TIMEOUT) as resp:  # noqa: S310
                if resp.status != 200:
                    return None
                return resp.read().decode("utf-8", "replace")
        except Exception:
            return None
