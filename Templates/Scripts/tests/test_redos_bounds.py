"""
test_redos_bounds.py -- the availability findings of Group F (Mythos review,
2026-10-20): patterns that were quadratic or worse on hostile text.

Each rewrite is held to two things:

  * a pumping input of 50k characters finishes in well under a second
    (the old forms took 3-13 s, or did not finish, on the same input);
  * on normal input it gives exactly what the old pattern gave. The old
    pattern is kept here verbatim and both are run over a seeded random
    corpus built from the characters that matter to each pattern.

Python 3.9 has no atomic groups or possessive quantifiers, so the rewrites
avoid ambiguity instead; where one could not keep the old result exactly,
the test names the difference.
"""
from __future__ import annotations

import random
import re
import time

import pytest

import markitdown_cleanup as mc
import morning_dashboard as md
import strip_ads
import templater_guard as tg
import url_safety
import vault_lint as vl

N = 50_000
BUDGET = 1.0


def _fast(fn, *args) -> None:
    t = time.perf_counter()
    fn(*args)
    took = time.perf_counter() - t
    assert took < BUDGET, f"{took:.2f}s on a {N}-char pumping input"


def _corpus(alphabet, count=4000, max_len=14, seed=20261020):
    rnd = random.Random(seed)
    for _ in range(count):
        yield "".join(rnd.choice(alphabet) for _ in range(rnd.randint(0, max_len)))


def _match_view(m):
    return None if m is None else (m.span(), m.groups())


# --------------------------------------------------------------------------
# morning_dashboard.TODO_RE (5268409)
# --------------------------------------------------------------------------

OLD_TODO = re.compile(r"^[ \t]*[-*+]\s*\[[ /]\]\s*(.+?)\s*$")
TODO_ALPHABET = ["- [ ] ", "- [/] ", " ", "\t", " ", "a", "#task", "x",
                 "\x0b", "]", "*", "\n"]


@pytest.mark.parametrize("line", [
    "- [ ] a" + " " * N + "b",
    "- [ ] " + "a " * (N // 2) + "\r",
    "- [ ] " + " " * N,
])
def test_todo_re_is_linear(line):
    _fast(md.TODO_RE.match, line)


def test_todo_re_matches_as_before():
    for line in _corpus(TODO_ALPHABET):
        assert _match_view(md.TODO_RE.match(line)) == \
            _match_view(OLD_TODO.match(line)), repr(line)


def test_todo_re_still_reads_a_task():
    m = md.TODO_RE.match("  - [ ]   Call the vendor #task  ")
    assert m.group(1) == "Call the vendor #task"


# --------------------------------------------------------------------------
# vault_lint (5268562)
# --------------------------------------------------------------------------

OLD_LIST_ITEM = re.compile(r"^\s*-\s+(.+?)\s*$")
OLD_WIKILINK = re.compile(r"(!?)\[\[([^\]\n]+)\]\]")
OLD_FM_SPLIT = re.compile(r"^(---\s*\n)(.*?)(\n---\s*\n?)(.*)$", re.DOTALL)


@pytest.mark.parametrize("line", ["- a" + " " * N + "b", "-" + " " * N])
def test_list_item_is_linear(line):
    _fast(vl._LIST_ITEM.match, line)


def test_list_item_matches_as_before():
    for line in _corpus(["-", " ", "\t", "a", "b", "\r", "'", "\n", " "]):
        assert _match_view(vl._LIST_ITEM.match(line)) == \
            _match_view(OLD_LIST_ITEM.match(line)), repr(line)


@pytest.mark.parametrize("line", ["[[" * (N // 2), "[[a" * (N // 3),
                                  "![[" * (N // 3)])
def test_wikilink_is_linear(line):
    _fast(vl._wikilinks, line)


def test_wikilinks_are_found_as_before():
    alphabet = ["[[", "]]", "[", "]", "!", "a", "|", "\n", "#"]
    for line in _corpus(alphabet, max_len=10):
        old = [(m.span(), m.groups()) for m in OLD_WIKILINK.finditer(line)]
        new = [(m.span(), m.groups()) for m in vl._wikilinks(line)]
        assert new == old, repr(line)

        def repl(m):
            if m.group(2) is None:
                return m.group(0)
            return "<" + m.group(2) + ">"
        assert vl._WIKILINK.sub(repl, line) == OLD_WIKILINK.sub(
            lambda m: "<" + m.group(2) + ">", line), repr(line)


@pytest.mark.parametrize("text", ["---" + "\n" * N + "x",
                                  "---\n" + "\n---x" * (N // 5)])
def test_vault_lint_frontmatter_split_is_linear(text):
    _fast(vl.split_frontmatter, text)


def test_vault_lint_frontmatter_split_as_before():
    alphabet = ["---", "-", "\n", " ", "\t", "\r", "a", "b: c", "\n---\n"]
    for text in _corpus(alphabet, count=6000):
        m = OLD_FM_SPLIT.match(text)
        assert vl.split_frontmatter(text) == (m.groups() if m else None), repr(text)


# --------------------------------------------------------------------------
# markitdown_cleanup (5278104)
# --------------------------------------------------------------------------

OLD_FRONTMATTER = re.compile(r"\A(---\s*\n.*?\n---\s*\n)", re.DOTALL)
OLD_STUB = re.compile(
    r"!\[(?P<alt>[^\]]*)\]"
    r"\(data:image/(?P<ext>png|jpe?g|gif|webp|bmp|svg)"
    r"(?:;[a-z0-9-]+)*;base64[^,)][^)]*\)", re.IGNORECASE)
OLD_B64 = re.compile(
    r"!\[(?P<alt>[^\]]*)\]"
    r"\(data:image/(?P<ext>png|jpe?g|gif|webp);base64,(?P<data>[^)]+)\)")


def _old_split(content):
    stripped = content.lstrip("﻿")
    m = OLD_FRONTMATTER.match(stripped)
    if m:
        return m.group(1), stripped[m.end():]
    return "", content


@pytest.mark.parametrize("text", ["---" + "\n" * N + "x",
                                  "---\n" + "\n---" * (N // 4) + "x"])
def test_cleanup_frontmatter_split_is_linear(text):
    _fast(mc.split_frontmatter, text)


def test_cleanup_frontmatter_split_as_before():
    alphabet = ["---", "-", "\n", " ", "\t", "\r", "a", "﻿", "\n---\n"]
    for text in _corpus(alphabet, count=6000):
        assert mc.split_frontmatter(text) == _old_split(text), repr(text)


@pytest.mark.parametrize("pattern, text", [
    ("STUB_IMAGE_RE", "![" * (N // 2)),
    ("STUB_IMAGE_RE", "![](data:image/png;base64x" * (N // 26)),
    ("B64_IMAGE_RE", "![" * (N // 2)),
    ("B64_IMAGE_RE", "![](data:image/png;base64," * (N // 26)),
    ("STUB_OR_PLACEHOLDER_RE", "![" * (N // 2)),
])
def test_image_patterns_are_linear(pattern, text):
    _fast(getattr(mc, pattern).sub, "", text)


IMG_TOKENS = ["![", "]", "(data:image/png;base64,", "(data:image/PNG;base64...",
              "(data:image/jpeg;x-y;base64.", "QUJD", ")", "alt", " ", "\n", "["]


def _view(rx, text):
    return [(m.span(), m.groups()) for m in rx.finditer(text)]


def _uses_old_latitude(rx, text):
    """An old match whose alt text holds "[" or whose URL holds "("."""
    for m in rx.finditer(text):
        whole = m.group(0)
        if "[" in m.group("alt") or "(" in whole[whole.index("](") + 2:]:
            return True
    return False


def test_image_patterns_match_as_before_on_balanced_text():
    """The one difference: alt text holding an unbalanced "[" (or data
    holding "("), which the old classes allowed. Strings where the old
    pattern matched such a thing are left out; every other is identical."""
    compared = 0
    for text in _corpus(IMG_TOKENS, count=6000, max_len=8):
        for old, new in ((OLD_STUB, mc.STUB_IMAGE_RE), (OLD_B64, mc.B64_IMAGE_RE)):
            if _uses_old_latitude(old, text):
                continue
            compared += 1
            assert _view(new, text) == _view(old, text), repr(text)
    assert compared > 6000


def test_a_real_stub_and_blob_still_match():
    stub = "![Image](data:image/png;base64...)"
    blob = "![x](data:image/png;base64,QUJD)"
    assert mc.STUB_IMAGE_RE.fullmatch(stub)
    assert mc.B64_IMAGE_RE.fullmatch(blob).group("data") == "QUJD"


# --------------------------------------------------------------------------
# strip_ads (5268415)
# --------------------------------------------------------------------------

OLD_IFRAME = re.compile(
    r'<iframe\b[^>]*?'
    r'(?:title="3rd party ad content"|title="Advertisement"|aria-label="Advertisement")'
    r'[^>]*?>.*?</iframe>', re.IGNORECASE | re.DOTALL)
OLD_VIDEO = re.compile(r'<video\b[^>]*?title="Advertisement"[^>]*?>.*?</video>',
                       re.IGNORECASE | re.DOTALL)


@pytest.mark.parametrize("text", [
    '<iframe title="Advertisement" ' * (N // 31),
    '<iframe title="Advertisement">' * (N // 30),
    "<iframe" * (N // 7),
    '<video title="Advertisement">' * (N // 29),
])
def test_ad_patterns_are_linear(text):
    _fast(strip_ads.strip_ads, text)


def test_ad_patterns_remove_as_before():
    alphabet = ["<iframe", "<IFRAME", "<iframex", "<video", " ", ">", "\n", "x",
                ' title="Advertisement"', ' aria-label="advertisement"',
                ' title="3rd party ad content"', "</iframe>", "</IFRAME>",
                "</video>", "<", "/"]
    for text in _corpus(alphabet, count=6000, max_len=9):
        assert strip_ads.AD_IFRAME.subn("", text) == OLD_IFRAME.subn("", text), repr(text)
        assert strip_ads.AD_VIDEO.subn("", text) == OLD_VIDEO.subn("", text), repr(text)


# --------------------------------------------------------------------------
# templater_guard (5268418)
# --------------------------------------------------------------------------

OLD_INLINE = re.compile(r"(`+)(?=[\s﻿]*(?:\$?=|(?:INPUT|VIEW|BUTTON)\[))", re.I)
_Q = r'(?:[^"\\\r\n]|\\.)*'
OLD_ESCAPED_KEY = re.compile(
    rf'(")(?!​)(?={_Q}\\{_Q}"[ \t]*:)'
    rf'|(?:^|(?<=[\n\r]))([ \t]*\?[ \t]*")(?!​)(?={_Q}\\)')


def _old_escaped(text):
    return OLD_ESCAPED_KEY.sub(lambda m: (m.group(1) or m.group(2)) + tg.ZWSP, text)


@pytest.mark.parametrize("text", [
    "`" * N,
    "`" * (N // 2) + " " * (N // 2),
    '"' + "\\a" * (N // 2),
    '"\\a' * (N // 3) + '":',
    '"\\"' * (N // 3) + '":',
    '"' * N + '\\":',
    "? \"" + "\\a" * (N // 2),
])
def test_guard_is_linear(text):
    _fast(tg.neutralize, text)


def test_inline_matches_as_before():
    alphabet = ["`", "``", " ", "=", "$", "INPUT[", "view[", "﻿", "a", "\n"]
    for text in _corpus(alphabet):
        assert tg._INLINE.sub(lambda m: m.group(1) + tg.ZWSP, text) == \
            OLD_INLINE.sub(lambda m: m.group(1) + tg.ZWSP, text), repr(text)


def test_escaped_keys_defused_as_before():
    alphabet = ['"', "\\", "a", ":", " ", "\t", "\n", "\r", "?", tg.ZWSP, "x"]
    for text in _corpus(alphabet, count=20000, max_len=12):
        assert tg._defuse_escaped_keys(text) == _old_escaped(text), repr(text)


@pytest.mark.parametrize("text", [
    '"\\x65xcalidraw-plugin": parsed',
    '{"k\\u0041": 1}',
    '? "k\\x"\n: v',
    '"a\\": v',          # the old pattern took the trailing backslash too
])
def test_escaped_key_examples(text):
    assert tg._defuse_escaped_keys(text) == _old_escaped(text) != text


# --------------------------------------------------------------------------
# url_safety.redact_url (5278630)
# --------------------------------------------------------------------------

OLD_USERINFO = re.compile(r"[^/]*@")


def test_redact_path_is_linear():
    _fast(url_safety._redact_path_userinfo, "a" * N)
    _fast(url_safety.redact_url, "http:///" + "a" * N)


def test_redact_path_as_before():
    for path in _corpus(["/", "@", "a", ":", "%40"]):
        assert url_safety._redact_path_userinfo(path) == \
            OLD_USERINFO.sub("<redacted>@", path), repr(path)


def test_redact_url_still_drops_path_userinfo():
    out = url_safety.redact_url("http:///user:pass@host/p@x/q?q=1")
    assert out == "http:///<redacted>@host/<redacted>@x/q?…"
