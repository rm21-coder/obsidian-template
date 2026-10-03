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
                "meta-bind-embed", "meta-bind-js-view", "mdm", "excalidraw-script-install",
                "base")
PLUGIN_PREFIXES = ("tasks", "dataview", "meta-bind", "mdm", "excalidraw", "base")
# Everything JS trim() drops beyond space and tab; U+200B it keeps.
JS_WS = ("\n\r\v\f\u00a0\u1680\u2000\u2001\u2002\u2003\u2004\u2005\u2006\u2007"
         "\u2008\u2009\u200a\u2028\u2029\u202f\u205f\u3000\ufeff")


def plugin_fences(text: str) -> list[str]:
    """Languages a plugin would render, modelled on Obsidian (reviewed in its
    app.js, 2026-10-03). Over-approximates where a fence may start.

    Reading view: the info string is backslash-unescaped, then entity-decoded;
    the language is what follows up to the first space or tab; the block gets
    class "language-<that>", and the DOM splits a class attribute on LF, FF, CR
    and space, so every resulting "language-X" class counts. Live Preview: the
    raw text after the fence, up to the first character outside [\\w/+#-]."""
    langs = []
    for m in re.finditer(r"(?=(?:```|~~~)([^\r\n]*))", text):    # every position
        raw = m.group(1).lstrip("`~")
        info = html.unescape(re.sub(r"\\([!-/:-@\[-`{-~])", r"\1", raw))
        token = re.split(r"[ \t]", info.lstrip(" \t"), maxsplit=1)[0]
        for cls in re.split(r"[\n\r\f ]", "language-" + token):
            if cls.startswith("language-"):
                langs.append(cls[len("language-"):].lower())
        live = re.match(r"[ \t]*([\w/+#-]*)", raw).group(1)
        langs.append(live.lower())
        # and the looser reading earlier models used (JS trim, case-folded)
        langs.append((info.strip(" \t" + JS_WS).split() or [""])[0].lower())
    return [lang for lang in langs if lang.startswith(PLUGIN_PREFIXES)]


def block_queries(text: str) -> list[str]:
    """Code-block text Dataview would run as a query (its default
    inlineQueriesInCodeblocks): the block's text, JS-trimmed, starting with "="
    or "$=" -- Dataview reads the whole block, not one line. A block is taken
    to start after any line holding a fence, and after a blank line followed
    by an indented line; container prefixes ("> ") are dropped."""
    lines = re.sub(r"\r\n|\r", "\n", text).split("\n")    # as remark does first
    out = []
    for i, line in enumerate(lines):
        opener = re.search(r"```|~~~", line)
        prev = lines[i - 1] if i > 0 else ""
        # Indented code after a blank line, a heading or a rule, or straight
        # after a list marker (the item's content is then indented code).
        after_break = (not prev.strip(" \t>" + JS_WS)
                       or re.match(r" {0,3}(?:#|([-*_])(?:[ \t]*\1){2,}[ \t]*$)", prev.lstrip(">")))
        indented = (re.match(r"[> ]*(?: {4}|\t)", line) and after_break) \
            or re.match(r"[> ]*(?:[-*+]|\d{1,9}[.)])(?: {5,}|[ \t]*\t)", line)
        if opener:
            body = lines[i + 1:]
        elif indented:
            body = lines[i:]
        else:
            continue
        block = "\n".join(re.sub(r"^[ \t>]*(?:(?:[-*+]|\d{1,9}[.)])[ \t]+)?", "", b) for b in body)
        block = block.strip(" \t" + JS_WS)
        if block.startswith(("=", "$=")) and block[1:].strip(" \t" + JS_WS):
            out.append(block[:30])
    return out


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
    "```x\flanguage-{l}",
    "```x&#10;language-{l}",
    "```x&#12;language-{l}",
    "```x&#13;language-{l}",
    "```x&NewLine;language-{l}",
    "~~~x\\&#10;language-{l}",
])
def test_no_plugin_block_survives_in_any_form(lang: str, shape: str) -> None:
    first = lang[0]
    raw = shape.format(l=lang, L=lang.upper(), r=lang[1:], n=ord(first),
                       h=format(ord(first), "x"), name={"t": "#116;", "d": "#100;",
                                                       "m": "#109;", "e": "#101;", "b": "#98;"}[first])
    assert plugin_fences(raw), f"case does not reach the oracle: {raw!r}"
    out = G.neutralize(raw)
    assert not plugin_fences(out), out
    assert G.is_neutral(out) and G.neutralize(out) == out
    if "language-" not in shape:    # a smuggled class is rewritten, not just split
        assert out.replace(G.ZWSP, "") == raw          # reads the same


@pytest.mark.parametrize("raw", ["```meta\\-bind-button", "```excalidraw\\-script-install",
                                 "```&#116;as&#107;s"])
def test_escaped_language_names_are_caught(raw: str) -> None:
    """CommonMark decodes backslash escapes and entities in a fence's info."""
    assert plugin_fences(raw), raw
    assert not plugin_fences(G.neutralize(raw))


@pytest.mark.parametrize("raw", [
    "```text\n= this.file.name\n```", "~~~\n= [[Secret]].field\n~~~",
    "para\n\n    = this.file.name\n", "```\n\n  $= dv.pages()\n```",
    "> ```\n> = x\n> ```", "- ```\n  = x\n  ```", "```\n\t\ufeff= x",
    "```text\n\u2003= x\n```", "```text\n\u3000= x\n```", "```text\n\v= x\n```",
    "```text\n\f= x\n```", "```text\n\u2028= x\n```",
    "```text\n=\nthis.file.name\n```", "para\n\n    =\n    this.file.name\n",
    "```text\n=\n\n1 + 1\n```", "> ```\n> =\n> x",
    "```text\r= x\r```", "```text\r=\rthis.file.name\r```", "```text\r\n= x\r\n```",
    "# H\n    =\n    this.file.name\n", "***\n    =\n    this.file.name\n",
    "-     = x\n", "1.      = x\n", "1. \t= x", "-     =\n      this.file.name\n",
    "> # H\n>     =\n>     x",
])
def test_no_dataview_query_survives_inside_a_code_block(raw: str) -> None:
    assert block_queries(raw), f"case does not reach the oracle: {raw!r}"
    out = G.neutralize(raw)
    assert not block_queries(out), out
    assert G.is_neutral(out) and out.replace(G.ZWSP, "") == raw


def test_setext_headings_and_ordinary_equals_are_left_alone() -> None:
    for text in ("Title\n=====\n", "Title\n=\n", "a == b", "x = 1", "a\n  b = c",
                 "> Title\n> ===\n", "1 + 1 = 2", "Title\n  ===\n"):
        assert G.neutralize(text) == text, text


@pytest.mark.parametrize("raw", ['"excalidraw\\u002dplugin": parsed', '"e\\x78calidraw-plugin": parsed',
                                 '"excalidraw\\x2donload-script": x', '- "a\\tb": 1',
                                 '{"\\x65xcalidraw-plugin": parsed}', '? "\\x65xcalidraw-plugin"\n: parsed'])
def test_escaped_yaml_keys_are_defused(raw: str) -> None:
    """YAML double-quoted keys decode escapes, so a key can spell
    excalidraw-plugin without the text "excalidraw-plugin"."""
    import yaml
    out = G.neutralize("---\n" + raw + "\n---\n")
    keys = yaml.safe_load(out.split("---\n")[1])
    keys = keys[0] if isinstance(keys, list) else keys
    assert not any(str(k).startswith("excalidraw-") or k == "a\tb" for k in keys), keys
    assert G.is_neutral(out)


def test_escaped_yaml_values_are_left_alone() -> None:
    """Titles with escaped quotes are common; only keys are defused."""
    text = '---\ntitle: "Lunch \\"Q4\\" chat"\npeople:\n  - "[[A \\"B\\"]]"\n---\n'
    assert G.neutralize(text) == text


def test_excalidraw_drawing_keys_are_defused() -> None:
    raw = "---\nexcalidraw-plugin: parsed\nexcalidraw-onload-script: alert(1)\n---\n"
    out = G.neutralize(raw)
    assert "excalidraw-plugin" not in out and "excalidraw-onload-script" not in out
    assert G.is_neutral(out) and out.replace(G.ZWSP, "") == raw


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
            assert not block_queries(out), raw
            assert inline_triggers(out, lambda xs: [x.strip(" \t\n" + JS_WS) for x in xs]) == [], raw
            assert out.replace(G.ZWSP, "") == raw


def test_the_guard_source_is_ascii() -> None:
    """Invisible characters are spelled as escapes, so a reader sees what the
    patterns match."""
    src = (SCRIPTS / "templater_guard.py").read_text(encoding="utf-8")
    assert all(ord(c) < 128 for c in src)
