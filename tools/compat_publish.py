#!/usr/bin/env python3
"""Keep ONE bug report in sync with what compat.py currently finds.

WHY THIS IS NOT JUST "POST A COMMENT"
-------------------------------------
The first version commented on every run that found a break. In one day it left
**11 identical 3,000-character comments** on the same issue. That is not a warning
system; it is a mute button with extra steps. The next real finding would have been
scrolled past, which defeats the entire point of building it.

So:

  * **The issue body is the current truth.** It is edited in place, never appended to.
  * **Comments are a changelog of CHANGES.** A run whose findings are identical to the
    last one says nothing at all -- no comment, no edit, no notification.
  * A fingerprint of the findings (broken ref/module/problem triples only) is embedded
    in the body. It deliberately ignores things that move on their own -- healthy rows,
    the hardware-verified column, TrueNAS point releases -- so `TS-25.10.4` becoming
    `TS-25.10.5` is not news, and does not wake anybody up.

  * When everything is fixed, the issue is **closed** with a comment saying so.

Works against GitHub and Gitea, which differ only in the auth header and the issue
list URL. One implementation, so the two cannot drift.

    python3 tools/compat_publish.py --api <url> --token <tok> --matrix /tmp/matrix.json
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request

sys.path.insert(0, __file__.rsplit("/", 1)[0])

from compat import (  # noqa: E402
    extract_fingerprint,
    fingerprint,
    is_broken,
    render_issue,
)

TITLE = "TrueNAS compatibility: the patch's assumptions no longer hold"


def _call(url, token, method="GET", data=None):
    req = urllib.request.Request(
        url, method=method,
        headers={
            # Gitea wants `token <t>`; GitHub accepts `Bearer <t>`. GitHub also
            # accepts `token <t>`, so one header serves both.
            "Authorization": f"token {token}",
            "Content-Type": "application/json",
            "Accept": "application/vnd.github+json",
        },
        data=json.dumps(data).encode() if data else None,
    )
    with urllib.request.urlopen(req) as r:  # noqa: S310
        return json.load(r) if r.length != 0 else {}


def _same(a, b):
    """Is the issue body already what we would write?

    Compared after normalising line endings and trailing space: forges are free to
    round-trip `\r\n`, and a body that only "differs" by that would be rewritten on
    every single run -- a silent edit, but a pointless one that churns `updated_at`
    and makes the issue look freshly touched every morning.
    """
    def norm(s):
        return "\n".join(line.rstrip() for line in (s or "").replace("\r\n", "\n").split("\n")).strip()
    return norm(a) == norm(b)


def find_issue(api, token, title):
    """The LOWEST-numbered issue with this title, open or closed.

    Lowest, not "whichever the API returns first": two issues with the same title
    existed once (an earlier version put the ref list in the title, so the identity
    changed whenever that set changed), and an order-dependent pick would alternate
    between them -- reopening one while commenting on the other.

    Both forges list PRs alongside issues, but they SAY SO DIFFERENTLY: GitHub omits
    the `pull_request` key on a plain issue, Gitea sends it as `null`. Testing for the
    KEY therefore discards every Gitea issue as if it were a PR -- so this returned
    None on every Gitea run, and the bot filed a brand-new duplicate report each time
    instead of editing the one it already had. Test the VALUE; it is the only form
    that is true on both.
    """
    issues = _call(f"{api}/issues?state=all&per_page=100&limit=100", token)
    mine = [
        i for i in issues
        if i.get("title") == title and not i.get("pull_request")
    ]
    return min(mine, key=lambda i: i["number"]) if mine else None


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--api", required=True, help="…/repos/<owner>/<repo>")
    ap.add_argument("--token", required=True)
    ap.add_argument("--matrix", required=True, help="compat.py --matrix --json output")
    args = ap.parse_args(argv[1:])

    with open(args.matrix, encoding="utf-8") as fh:
        rows = json.load(fh)

    broken = [r for r in rows if any(is_broken(m) for m in r["modules"].values())]
    issue = find_issue(args.api, args.token, TITLE)

    # ── everything is healthy ────────────────────────────────────────────────
    if not broken:
        if issue and issue["state"] == "open":
            _call(f"{args.api}/issues/{issue['number']}/comments", args.token, "POST",
                  {"body": "All of the patch's assumptions hold again on every "
                           "checked TrueNAS version. Closing."})
            _call(f"{args.api}/issues/{issue['number']}", args.token, "PATCH",
                  {"state": "closed"})
            print(f"closed #{issue['number']} — nothing is broken any more")
        else:
            print("nothing broken; no open report to close")
        return 0

    body = render_issue(rows)
    want = fingerprint(rows)

    # ── nothing to file yet ──────────────────────────────────────────────────
    if issue is None:
        made = _call(f"{args.api}/issues", args.token, "POST",
                     {"title": TITLE, "body": body})
        print(f"filed #{made['number']}")
        return 0

    have = extract_fingerprint(issue.get("body") or "")
    n = issue["number"]

    # ── two different questions, and they were being answered with one answer ────
    #
    #   * IS THE BODY STILL TRUE?     -> if not, rewrite it. Editing an issue body
    #     notifies NOBODY on either forge, so keeping it honest is free.
    #   * HAVE THE FINDINGS CHANGED?  -> only then comment. Comments DO notify, and a
    #     daily "still broken, same as yesterday" is what teaches everyone to ignore
    #     the one that finally matters.
    #
    # Conflating them meant an unchanged FINGERPRINT froze the BODY. The fingerprint
    # deliberately ignores everything that moves on its own -- healthy rows, the
    # hardware-verified column, point releases, how a row is LABELLED -- so none of
    # that could ever reach the report. Relabelling master `27-dev` (it is not the
    # next release; a red row there was reading as "the version you are about to
    # install is broken") would have shipped to the README and never to the issue
    # anybody actually opens.
    body_is_current = _same(issue.get("body"), body)

    if have == want and issue["state"] == "open" and body_is_current:
        print(f"#{n} is already current ({want}) — staying quiet")
        return 0

    _call(f"{args.api}/issues/{n}", args.token, "PATCH", {"body": body, "state": "open"})

    if have != want:
        refs = ", ".join(f"`{r['ref']}`" for r in broken)
        note = (
            "The findings changed — the report above has been updated.\n\n"
            f"Currently broken on: {refs}."
            if have else
            "This report is now kept up to date automatically: the body above always "
            "reflects the current findings, and a comment is only added when they "
            "change."
        )
        _call(f"{args.api}/issues/{n}/comments", args.token, "POST", {"body": note})
        print(f"updated #{n}: {have} -> {want}")
    elif issue["state"] != "open":
        print(f"reopened #{n}")
    else:
        # Same findings, new rendering. Silent by design: nothing has changed that
        # anybody needs waking up for, but the report should not be telling lies.
        print(f"#{n}: findings unchanged ({want}); body refreshed silently")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
