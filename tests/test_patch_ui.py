"""Tests for the Angular bundle patch.

This is the one part of the patch that edits *minified third-party JavaScript* by
regex, so it is the easiest place to silently produce a broken bundle: a pattern
that matches nothing leaves the dropdown Storj-only, and a pattern that matches
sloppily can unbalance the parentheses and take the whole web UI down.

Nothing checked it until now. The snippets below are verbatim from a real
TrueNAS 25.x bundle (chunk-*.js, pre-patch).
"""

import os
import re
import sys

import pytest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "patch"))

from patch_ui import MARKER, _PATTERNS, _match_pattern  # noqa: E402

# Verbatim from /usr/share/truenas/webui/chunk-FX2QXNQU.js on TrueNAS 25.10.
# Angular emits the binding as a chained ɵɵproperty(...)(...) call, so the
# pureFunction call is followed by TWO closing parens: one for pe(...), one for
# property(...).
REAL_25X = (
    'c(2,"filterByProviders",pe(115,Rn,i.CloudSyncProviderName.Storj))'
    '("required",!0),r(3'
)

# TrueNAS 24.x and earlier emitted a literal array.
REAL_24X = 'c(2,"filterByProviders",["STORJ_IX"])("required",!0),r(3'


def apply_patch(content):
    """Run the same match-and-substitute main() does."""
    find, replace = _match_pattern(content)
    assert find is not None, "no pattern matched"
    patched, count = find.subn(replace, content)
    return patched, count


def paren_delta(s):
    """Net paren balance. The snippets are fragments of a minified file, so they
    are not balanced on their own -- what must hold is that patching does not
    CHANGE the balance. Consuming one paren too many is a syntax error in the
    bundle, and the whole TrueNAS web UI goes blank."""
    return s.count("(") - s.count(")")


@pytest.mark.parametrize("source", [REAL_25X, REAL_24X], ids=["25.x", "24.x"])
class TestAgainstRealBundles:
    def test_matches_exactly_once(self, source):
        # main() refuses to write unless count == 1 — more than one match would
        # mean the pattern is too loose to trust against a minified bundle.
        _patched, count = apply_patch(source)
        assert count == 1

    def test_result_contains_all_three_providers(self, source):
        patched, _ = apply_patch(source)
        assert MARKER in patched
        assert '"filterByProviders",["STORJ_IX","S3","B2"]' in patched

    def test_patch_does_not_change_paren_balance(self, source):
        # Consuming one paren too many (or too few) is a syntax error in the
        # bundle and the entire TrueNAS web UI goes blank. This is the invariant
        # the 25.x pattern has to get right: it eats `pe(...)` which sits inside
        # a chained property(...)(...) call.
        patched, _ = apply_patch(source)
        assert paren_delta(patched) == paren_delta(source)

    def test_surrounding_code_is_untouched(self, source):
        patched, _ = apply_patch(source)
        assert patched.startswith("c(2,")
        assert patched.endswith('("required",!0),r(3')

    def test_patch_is_idempotent(self, source):
        # apply.sh re-runs every boot; MARKER short-circuits an already-patched
        # file, but the pattern must also not match its own output.
        patched, _ = apply_patch(source)
        find, _replace = _match_pattern(patched)
        if find is not None:
            # Only the 24.x literal-array pattern may still "match" — and only if
            # it would produce the same text. Anything else means double-patching.
            again, _ = apply_patch(patched)
            assert again == patched, "re-patching must be a no-op"


def test_storj_only_bundle_is_recognised():
    assert _match_pattern(REAL_25X)[0] is not None


def test_unrelated_javascript_is_never_touched():
    # A pattern loose enough to hit unrelated code would corrupt the bundle.
    for noise in (
        'c(2,"filterByProviders",pe(115,Rn,i.SomethingElse.Storj))',
        'c(2,"otherBinding",pe(115,Rn,i.CloudSyncProviderName.Storj))',
        '"filterByProviders"',
    ):
        find, _ = _match_pattern(noise)
        assert find is None, f"pattern must not match: {noise}"


def test_every_pattern_is_anchored_to_filterbyproviders():
    # Guards against a future pattern broad enough to rewrite arbitrary JS.
    for find, _replace in _PATTERNS:
        assert "filterByProviders" in find.pattern


def test_patterns_compile_and_replacements_reference_group_one():
    for find, replace in _PATTERNS:
        assert isinstance(find, re.Pattern)
        assert r"\1" in replace, "replacement must preserve the binding name"
