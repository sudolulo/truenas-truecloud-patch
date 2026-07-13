# Development and releasing

> Part of [truenas-truecloud-patch](../README.md).

## Development

Parts of this project were written with AI assistance (Claude). All of it is
reviewed and tested before release; the test suite and CI exist in large part to
make that review meaningful. Bugs are mine.

```bash
pip install pytest ruff
ruff check patch tests tools
pytest tests
```

CI runs shellcheck, `bash -n`, ruff, and pytest on Python 3.11–3.13. Two checks
are worth calling out, because nothing else would catch what they catch:

- The tests **`compile()` the `*_BLOCK` strings** in `patch/apply.sh`. Those are
  Python source appended into live `middlewared` modules — a syntax error there
  breaks the box at boot, and they're string literals, so nothing else type-checks
  them.
- CI asserts **every script declares the same version**, and that it matches the
  newest CHANGELOG entry. `VERSION=` had silently drifted to three different
  values across the scripts before anything checked.

The project is hosted on **Gitea** (`git.onetick.ninja/flan/truenas-truecloud-patch`)
and mirrored to GitHub. Both run the same workflows — Gitea reads
`.github/workflows/` too — so a change is checked twice, on two independent runners.

---

## Releasing

**Every release interrupts every user.** An update alert fires on each installed
box (see [Update alerts](../README.md#update-alerts)), so a release that exists only to fix the
last release teaches people to dismiss the alert — and one day that alert will be
carrying a security fix. This project cut twelve releases in a single day once.
Never again, and not by good intentions: by a gate.

### The rule

> A stable `vX.Y.Z` may only be published if a `vX.Y.Z-rcN` tag points at the
> **same commit**.

Release candidates are **invisible to users**: `update.sh` and the update alert both
take the newest plain `vX.Y.Z` tag, so an `-rc` is never offered as an update. All
the debugging therefore happens across `rc1`, `rc2`, `rc3` — at nobody's expense —
instead of across `v0.5.0`, `v0.5.1`, `v0.5.2`, at everybody's.

"The candidate passed, then I pushed one more little fix" is refused **by name**.
That is not hypothetical; it is exactly how v0.5.1 happened.

### Day to day

You don't touch the release machinery. Write your changes under `## Unreleased` in
`CHANGELOG.md` and push to `main`. `main` is a work surface — it is allowed to be
mid-thought. Releasing is a separate, deliberate act.

### Cutting a release

```bash
bash release.sh 0.6.0 --check      # what would ship? what is the next rc?

bash release.sh 0.6.0 --rc         # promotes `## Unreleased` -> v0.6.0, stamps every
                                   # VERSION=, tags v0.6.0-rc1, pushes. Users see nothing.

#   ... install it on a real box. Exercise it. Break it. ...
#   Found a bug? Fix it on main, then `bash release.sh 0.6.0 --rc` again -> rc2.

bash release.sh 0.6.0 --promote    # publishes v0.6.0. REFUSED unless an rc points here.
```

### The gates, and where they live

The logic is Python so it can be unit-tested; CI is the enforcement boundary
because it is the only actor holding the token that publishes. `release.sh` runs the
**same** code locally so you fail in 200 ms instead of after a push.

| gate | enforces | where |
| --- | --- | --- |
| [`release_notes.py check`](../tools/release_notes.py) | every script's `VERSION=` matches the tag; the CHANGELOG section exists and is non-empty; **nothing is stranded under `## Unreleased`** | `release.sh` + CI |
| [`release_gate.py`](../tools/release_gate.py) | **an rc points at this exact commit** | `release.sh` + CI |
| the full suite | ruff, pytest, shellcheck, `bash -n` — re-run against the *tagged* commit | CI |

A release's body **is** its `CHANGELOG.md` section — there is no second place to
write release notes, and therefore no second place for them to go stale. Releases
are published on both forges.

### Why the alert doesn't nag

A release whose CHANGELOG contains only a `### Docs` section changed no code, and
raises **no alert**. Candidates raise no alert either. So the only thing that ever
interrupts a user is a real, complete change — which is the entire point.

---

