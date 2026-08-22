"""meeting_prepopulate: the reschedule-banner machinery.

This file exists because commit 85740af deleted the RESCHEDULE_FENCE_*
constants while the banner code still used them, and no test noticed — the
first rescheduled meeting under the nightly cadence would have crashed the
consumer with NameError. These tests import the module for real and drive
the banner round-trip, so a repeat of that failure class dies in CI, not
at 05:00.
"""
from __future__ import annotations

import pytest

import meeting_prepopulate as mp


class TestRescheduleFences:

    def test_fence_constants_exist_and_are_html_comments(self) -> None:
        assert mp.RESCHEDULE_FENCE_START.startswith("<!--")
        assert mp.RESCHEDULE_FENCE_END.startswith("<!--")
        assert mp.RESCHEDULE_FENCE_START != mp.RESCHEDULE_FENCE_END
        # plistlib-adjacent lesson does not apply here, but XML-comment
        # rules do: no '--' inside the comment body.
        for fence in (mp.RESCHEDULE_FENCE_START, mp.RESCHEDULE_FENCE_END):
            inner = fence[4:-3]
            assert "--" not in inner, f"'--' inside comment body: {fence!r}"

    def test_banner_is_fenced(self) -> None:
        banner = mp._reschedule_banner("2026-08-22 09:00", "2026-08-22 14:00",
                                       "2026-08-22T06:00:00Z")
        assert banner.startswith(mp.RESCHEDULE_FENCE_START)
        assert banner.rstrip().endswith(mp.RESCHEDULE_FENCE_END)
        assert "2026-08-22 14:00" in banner

    def test_inject_banner_is_idempotent(self) -> None:
        """Injecting a second banner replaces the first — a meeting
        rescheduled twice carries one banner, not a stack."""
        body = "# Meeting\n\nnotes here\n"
        b1 = mp._reschedule_banner("a", "b", "t1")
        first = mp._inject_banner(body, b1)
        assert first.count(mp.RESCHEDULE_FENCE_START) == 1
        b2 = mp._reschedule_banner("b", "c", "t2")
        second = mp._inject_banner(first, b2)
        assert second.count(mp.RESCHEDULE_FENCE_START) == 1
        assert "notes here" in second

    def test_module_has_no_unresolved_names(self) -> None:
        """AST guard: every bare Name loaded at module scope inside function
        bodies must resolve to a module attribute, builtin, local, or
        argument is too strict to check cheaply — so pin the specific
        regression instead: the two fence names must be module attributes."""
        for name in ("RESCHEDULE_FENCE_START", "RESCHEDULE_FENCE_END"):
            assert hasattr(mp, name)
