"""
test_templater_guard.py -- text from outside the vault must not run as Templater code.

Templater runs dynamic commands (<%+ %>, <%*+ %> as JavaScript) in the rendered
text of every note shown in reading view, with no setting or folder limit, and
runs every command in a new note when its creation trigger is on. The scripts
write invite subjects, attendee names, clipped pages, transcripts and converted
documents into notes (found 2026-10-01). templater_guard.neutralize() splits
the opener with a zero-width space; these pin that it does, in every rendered
form, and that every writer of outside text applies it.
"""
from __future__ import annotations

import html
import re
from pathlib import Path

import pytest

import templater_guard as G

SCRIPTS = Path(G.__file__).resolve().parent

# What Templater's dynamic post-processor matches, applied to RENDERED text.
TEMPLATER_DYNAMIC = re.compile(r"(<%(?:-|_)?\s*[*~]{0,1})\+((?:.|\s)*?%>)")
TEMPLATER_ANY = re.compile(r"<%")


def rendered(text: str) -> str:
    """Roughly what the DOM text holds: entities decoded, markdown escapes gone."""
    return re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", html.unescape(text))


@pytest.mark.parametrize("raw", [
    "<%+ tp.file.include('[[People/Secret]]') %>",
    "<%*+ require('child_process') %>",
    "<%- *+ x %>",
    "&lt;%+ x %>",
    "&lt%+ x %>",
    "&#60;%+ x %>",
    "&#x3C;%+ x %>",
    "<&#37;+ x %>",
    "<&percnt;+ x %>",
    "\\<%+ x %>",
    "<\\%+ x %>",
    "\\<\\%*+ x %>",
    "&LT;&#X25;+ x %>",
    "<% tp.date.now() %>",
])
def test_every_rendered_form_of_the_opener_is_split(raw: str) -> None:
    out = G.neutralize(raw)
    assert not TEMPLATER_DYNAMIC.search(rendered(out)), out
    assert not TEMPLATER_ANY.search(rendered(out)), out
    assert G.is_neutral(out)


def test_it_is_idempotent_and_leaves_ordinary_text_alone() -> None:
    text = "50% < 75% and 3 %> 2; a <b>tag</b>"
    assert G.neutralize(text) == text
    once = G.neutralize("<%+ x %>")
    assert G.neutralize(once) == once
    assert once.replace(G.ZWSP, "") == "<%+ x %>"      # reads the same


def test_the_clipping_cleaner_neutralizes_a_clipped_page(tmp_path: Path) -> None:
    import strip_ads
    clip = tmp_path / "Clip.md"
    clip.write_text("---\ntitle: x\n---\n\nA page. <%*+ require('child_process') %>\n",
                    encoding="utf-8")
    assert strip_ads.process_file(clip, dry_run=False, quiet=True, verbose=False)
    assert not TEMPLATER_ANY.search(rendered(clip.read_text(encoding="utf-8")))


def test_a_classifier_rationale_is_neutralized() -> None:
    import classify_notes
    assert G.is_neutral(classify_notes.yaml_quote("page says <%+ tp.file.include('x') %>"))


# Every place a script writes outside text into a note.
WRITERS = {
    "meeting_prepopulate.py": [
        "templater_guard.neutralize(_yaml_email(email) or '')",
        "path.write_text(templater_guard.neutralize(content), encoding='utf-8')",
    ],
    "youtube_summarize.py": ['templater_guard.neutralize(frontmatter + "\\n" + body)'],
    "podcast_transcribe.py": ['templater_guard.neutralize("\\n".join(lines) + "\\n")'],
    "voice_cleanup.py": ["templater_guard.neutralize(note_content)"],
    "markitdown_convert.py": ["templater_guard.neutralize(body)"],
    "markitdown_dropper.py": ["templater_guard.neutralize(body)"],
    "markitdown_cleanup.py": ["cleaned = templater_guard.neutralize(cleaned)"],
    "strip_ads.py": ["templater_guard.neutralize(strip_ads(original))"],
    "meeting_prep.py": ["templater_guard.neutralize(build_block(now, mode, attendee_tasks))"],
    "tag_clippings.py": ["line = templater_guard.neutralize("],
    "classify_notes.py": ["flat = templater_guard.neutralize(flat)"],
    "meeting_group_backfill.py": ["text = templater_guard.neutralize('\\n'.join(out))"],
    "obsidian-rag-sync.py": ['templater_guard.neutralize("\\n".join(lines))'],
}


@pytest.mark.parametrize("script", sorted(WRITERS))
def test_every_writer_of_outside_text_applies_the_guard(script: str) -> None:
    src = (SCRIPTS / script).read_text(encoding="utf-8")
    for needle in WRITERS[script]:
        assert needle in src, f"{script}: {needle}"


def test_meeting_prepopulate_guards_every_new_note_it_writes() -> None:
    """New meeting notes, People stubs and series roots: three writes."""
    src = (SCRIPTS / "meeting_prepopulate.py").read_text(encoding="utf-8")
    assert src.count("path.write_text(templater_guard.neutralize(content), encoding='utf-8')") == 3


# ---- plugin code triggers (2026-10-03) -------------------------------------
# Tasks runs `filter/sort/group by function` lines as JavaScript in any
# rendered ```tasks block, with no setting to stop it; Dataview runs `= ...`
# inline queries by default and renders the result as markdown; Meta Bind,
# Metadata Menu and Excalidraw render blocks that act on click. Outside text
# must reach none of them.

import itertools
import json
import shutil
import subprocess

PLUGIN_LANGS = ("tasks", "dataview", "dataviewjs", "meta-bind", "meta-bind-button",
                "meta-bind-embed", "meta-bind-js-view", "mdm", "excalidraw-script-install")
JS_WS = "\u00a0\u2003\u3000\ufeff\u2028"   # JS trim() drops these; U+200B it keeps


def plugin_fences(text: str) -> list[str]:
    """Languages a plugin would render: every fence run's info string, decoded
    as CommonMark decodes it, first word, case-folded. Over-approximates where a
    fence may start (the plugins only ever see a subset)."""
    langs = []
    for m in re.finditer(r"(?=(?:```|~~~)([^\r\n]*))", text):    # every position
        info = html.unescape(re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", m.group(1).lstrip("`~")))
        word = (info.strip(" \t" + JS_WS).split() or [""])[0].lower()
        if word.startswith(("tasks", "dataview", "meta-bind", "mdm", "excalidraw")):
            langs.append(word)
    return langs


@pytest.mark.parametrize("lang", PLUGIN_LANGS)
@pytest.mark.parametrize("shape", [
    "```{l}\nfilter by function task.x\n```",
    "~~~~{l}\nx\n~~~~",
    "   ```  {l}",
    "- ```{l}",
    "1. ```{l}",
    "> > ```{l}",
    "> - ```{l}",
    "```\t{l} extra words",
    "```{L}",
    "``` \ufeff{l}",
    "```\u00a0{l}",
    "```&#{n};{r}",
    "```&#x{h};{r}",
    "```&{name}{r}",
    "text ```` ```{l}",
])
def test_no_plugin_block_survives_in_any_form(lang: str, shape: str) -> None:
    first = lang[0]
    raw = shape.format(l=lang, L=lang.upper(), r=lang[1:], n=ord(first),
                       h=format(ord(first), "x"), name={"t": "#116;", "d": "#100;",
                                                       "m": "#109;", "e": "#101;"}[first])
    assert plugin_fences(raw), f"case does not reach the oracle: {raw!r}"
    out = G.neutralize(raw)
    assert not plugin_fences(out), out
    assert G.is_neutral(out) and G.neutralize(out) == out
    assert out.replace(G.ZWSP, "") == raw          # reads the same


@pytest.mark.parametrize("raw", ["```meta\\-bind-button", "```excalidraw\\-script-install",
                                 "```&#116;as&#107;s"])
def test_escaped_language_names_are_caught(raw: str) -> None:
    """CommonMark decodes backslash escapes and entities in a fence's info."""
    assert plugin_fences(raw), raw
    assert not plugin_fences(G.neutralize(raw))


def test_ordinary_code_blocks_are_left_alone() -> None:
    for lang in ("python", "js", "json", "bash", "mermaid", "query", "text", ""):
        text = f"```{lang}\nx = 1\n```\n~~~{lang}\n~~~"
        assert G.neutralize(text) == text, lang


def _js_trim(values: list[str]) -> list[str]:
    out = subprocess.run([shutil.which("node"), "-e",
                          "process.stdout.write(JSON.stringify(JSON.parse("
                          "require('fs').readFileSync(0,'utf8')).map(s=>s.trim())))"],
                         input=json.dumps(values), capture_output=True, text=True, check=True)
    return json.loads(out.stdout)


def inline_triggers(text: str, trim) -> list[str]:
    """Inline code a plugin would act on: the code span's text, trimmed the way
    Dataview and Meta Bind trim it, starting with one of their prefixes."""
    spans = [m.group(2) for m in re.finditer(r"(`+)(.+?)\1", text, re.S)]
    spans += re.findall(r"<code[^>]*>(.*?)</code>", text, re.I | re.S)
    spans = [html.unescape(re.sub(r"<[^>]+>", "", s)) for s in spans]
    return [s for s in trim(spans)
            if s.startswith(("=", "$=")) or re.match(r"(INPUT|VIEW|BUTTON)\[", s, re.I)]


INLINE_CASES = [
    "`= this.file.name`", "`$= dv.pages()`", "`` = x ``", "```= x```",
    "` \u00a0= x`", "`\ufeff$=x`", "`\u3000INPUT[text:x]`", "`VIEW[{x}]`",
    "`button[b]`", "a `= [[Secret]].body` b", "`\n= x`",
    "<code>= x</code>", "<CODE class='a'>$=x</CODE>", "<code><b></b>= x</code>",
    "<code>&#61; x</code>", "<code>&equals; x</code>",
]


@pytest.mark.skipif(shutil.which("node") is None, reason="node not installed")
@pytest.mark.parametrize("raw", INLINE_CASES)
def test_no_inline_plugin_code_survives(raw: str, allow_subprocess) -> None:
    assert inline_triggers(raw, _js_trim), f"case does not reach the oracle: {raw!r}"
    out = G.neutralize(raw)
    assert not inline_triggers(out, _js_trim), out
    assert G.is_neutral(out) and G.neutralize(out) == out


def test_ordinary_inline_code_is_left_alone() -> None:
    text = "use `a = b`, `x == y`, `INPUTS`, `view(x)` and <codex> or <code-block>"
    assert G.neutralize(text) == text


def test_mixed_triggers_fuzz() -> None:
    """Every pairing of trigger, wrapper and position, in one text."""
    pieces = ["```tasks\nfilter by function true\n```", "`= x`", "<code>$=x</code>",
              "<%+ x %>", "~~~ dataviewjs", "`INPUT[a]`", "plain text"]
    for a, b in itertools.permutations(pieces, 2):
        for joint in ("\n", " ", "\n> ", "\n- "):
            raw = a + joint + b
            out = G.neutralize(raw)
            assert G.is_neutral(out), raw
            assert not plugin_fences(out), raw
            assert inline_triggers(out, lambda xs: [x.strip(" \t\n" + JS_WS) for x in xs]) == [], raw
            assert out.replace(G.ZWSP, "") == raw
