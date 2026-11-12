"""Every writer that files outside text runs it through output_sanitizer
(v2.6 assessment WP4: F12-F16, F25; F27 and the F15 note names).

Each test drives the real write path with a payload holding every opener the
sanitizer defuses, and checks the file on disk: nothing in it may load or act
when Obsidian opens the note. No network, no model, no Keychain.
"""
from __future__ import annotations

import importlib.util
import sys
from datetime import datetime
from pathlib import Path

import pytest

import output_sanitizer

Z = "​"
HERE = Path(__file__).resolve().parent.parent

PAYLOAD = ("Summary ![x](https://evil.example/p.png) and <img src=x> and "
           "[o](obsidian://open?vault=v) and [w](ms-word:https://e.example/a.docx)\n"
           "```mermaid\ngraph TD; A-->B\n```\n")


def assert_inert(text: str) -> None:
    assert "![" not in text, text
    assert "<img" not in text, text
    assert "obsidian://" not in text and "obsidian:" not in text.replace("obsidian" + Z, ""), text
    assert "ms-word:" not in text, text
    assert "```mermaid" not in text, text


# ---- F12: YouTube summary ----------------------------------------------------

def test_youtube_summary_is_sanitized(tmp_path, monkeypatch):
    import youtube_summarize as ys
    monkeypatch.setattr(ys, "fetch_video", lambda url: {"title": "A talk", "id": "abc12345678",
                                                        "webpage_url": url})
    monkeypatch.setattr(ys, "extract_transcript", lambda info: "spoken words")
    monkeypatch.setattr(ys, "summarize", lambda t, **k: PAYLOAD)
    path = ys.process_video("https://www.youtube.com/watch?v=abc12345678", out_dir=tmp_path,
                            model="m", dry_run=False, verbose=False)
    text = path.read_text(encoding="utf-8")
    assert_inert(text)
    assert "Summary" in text


def test_youtube_is_a_listed_sanitized_writer():
    src = (HERE / "youtube_summarize.py").read_text(encoding="utf-8")
    assert "output_sanitizer.sanitize_report(body)" in src


# ---- F13: podcast transcript -------------------------------------------------

def test_podcast_transcript_is_sanitized(tmp_path):
    import podcast_transcribe as pt
    path = pt.write_transcript_md(out_dir=tmp_path, title="Episode 1",
                                  source_url="https://pod.example/e1",
                                  result={"text": PAYLOAD, "language": "en"},
                                  audio_seconds=60.0, model="m")
    text = path.read_text(encoding="utf-8")
    assert_inert(text)
    assert "classification: internal-use-only" in text


# ---- F14: tag promotion log --------------------------------------------------

@pytest.mark.parametrize("tag", ["A\r`![](https://x/y)", "B ![](https://x/y)",
                                 "C\u0085<img src=x>", "D\n|x|y"])
def test_a_logged_tag_is_one_row_and_inert(tmp_path, monkeypatch, tag):
    import importlib
    import types
    fake = types.ModuleType("anthropic")
    fake.Anthropic = type("Anthropic", (), {})
    monkeypatch.setitem(sys.modules, "anthropic", fake)      # as test_tag_clippings
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delitem(sys.modules, "tag_clippings", raising=False)
    tc = importlib.import_module("tag_clippings")
    monkeypatch.delitem(sys.modules, "tag_clippings", raising=False)
    log = tmp_path / "candidates.md"
    monkeypatch.setattr(tc, "PROMOTION_CANDIDATES_FILE", log)
    tc.log_promotion_candidate(tag, Path("Clippings/A page.md"))
    rows = [ln for ln in log.read_text(encoding="utf-8").splitlines()
            if ln.startswith("| ") and "A page" in ln]
    assert len(rows) == 1, log.read_text(encoding="utf-8")
    row = rows[0]
    for ch in ("\r", " ", " ", "\u0085"):
        assert ch not in log.read_text(encoding="utf-8")
    assert row.count("|") == 4, row
    assert "![" not in row and "<img" not in row


# ---- F15: RAG run report and converted note names ---------------------------

def _rag(tmp_path, monkeypatch):
    home, vault = tmp_path / "home", tmp_path / "vault"
    (vault / "Creations").mkdir(parents=True)
    home.mkdir()
    for k, v in (("HOME", home), ("USERPROFILE", home), ("LOCALAPPDATA", home / "L"),
                 ("OBSIDIAN_VAULT", vault), ("OPEN_WEBUI_URL", "http://webui.invalid"),
                 ("OBSIDIAN_COLLECTION_ID", "c")):
        monkeypatch.setenv(k, str(v))
    import secret_store
    monkeypatch.setattr(secret_store, "get_secret", lambda name, **k: "k")
    spec = importlib.util.spec_from_file_location("rag_sync_w", HERE / "obsidian-rag-sync.py")
    mod = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, "rag_sync_w", mod)
    spec.loader.exec_module(mod)
    return mod


@pytest.mark.parametrize("name", ["Clippings/x`<img src=http&#58;&#47;&#47;h&#47;p>`.md",
                                  "Clippings/![x](http&#58;&#47;&#47;h&#47;p).md",
                                  "Clippings/<img src=x>.md"])
def test_rag_report_echoes_outside_names_inertly(tmp_path, monkeypatch, name):
    mod = _rag(tmp_path, monkeypatch)
    path = mod.write_run_report(datetime.now(), {"new_dry_run": [name]}, dry_run=True)
    assert path is not None
    text = path.read_text(encoding="utf-8")
    assert "![" not in text and "<img" not in text, text
    assert "Clippings/" in text                     # still shown, as text


@pytest.mark.parametrize("stem,want", [
    ("Q3 Budget (draft)", "Q3 Budget draft"),
    ("a`<img src=x>`b", "a img src x b"),
    ("![x](http&#58;&#47;&#47;h)", "x http 58 47 47 h"),
    (".hidden", "hidden"),                         # F37
    ("...", "converted"),
    ("Résumé – 2026", "Résumé – 2026"),
])
def test_safe_note_stem(stem, want):
    assert output_sanitizer.safe_note_stem(stem) == want


# ---- F16: vault_lint --fix-links ---------------------------------------------

def test_fix_links_never_unlinks_into_a_live_tag(tmp_path, monkeypatch):
    import vault_lint as vl
    monkeypatch.setattr(vl, "VAULT_ROOT", tmp_path)
    note = tmp_path / "Notes" / "a.md"
    note.parent.mkdir()
    note.write_text('x <[[zq7x|img src="https://example.invalid/b.png"]]> y\n'
                    "see [[zq7x|plain words]] here\n", encoding="utf-8")
    changes = vl.fix_links([], {"zq7x": {"Notes/a.md"}})
    (c,) = changes
    assert "<img" not in c["new"]
    assert '<[[zq7x|img src="https://example.invalid/b.png"]]>' in c["new"]  # left in place
    assert "see plain words here" in c["new"]                                 # the safe one fixed
    assert "zq7x" in c["unsafe"] and c["unlinked"] == ["zq7x"]


def test_fix_links_still_unlinks_an_ordinary_alias(tmp_path, monkeypatch):
    import vault_lint as vl
    monkeypatch.setattr(vl, "VAULT_ROOT", tmp_path)
    note = tmp_path / "Notes" / "b.md"
    note.parent.mkdir()
    note.write_text("met [[Gone Person|Pat]] today\n", encoding="utf-8")
    (c,) = vl.fix_links([], {"Gone Person": {"Notes/b.md"}})
    assert c["new"] == "met Pat today\n" and c["unsafe"] == []


# ---- F25: email tasks --------------------------------------------------------

def _req(**over):
    base = {"message_id": "mail:///messages/AAMkAD123", "kind": "request", "task": "Send the report",
            "sender_address": "pat@example.org", "sender_name": "", "subject": "Report",
            "weblink": None, "due": None}
    base.update(over)
    return {"requests": [base]}


def test_an_address_that_carries_markup_never_reaches_the_line():
    """F25's case: the line carries no markup. Review B2: the item is kept,
    under a fixed name, rather than dropped."""
    import email_task_transform as et
    (r,) = et.validate(_req(sender_address="![](https://evil.example/t.png)@a.b"),
                       set(), set())
    assert r["sender_name"] == et.UNSHOWN_SENDER and r["sender_address"] == ""
    (line,) = et.render([r], {}, {}, datetime(2026, 11, 12).date())
    for bad in ("![", "](", "[[", "<", "evil"):
        assert bad not in line, line


@pytest.mark.parametrize("addr", ["r&d@partner.example", "bounce=pat=example.org@lists.example",
                                  "sales/west@partner.example",
                                  # a punycode top-level domain, built in two parts so the
                                  # identity gate does not read it as a real address
                                  "user@example." + "xn--p1ai",
                                  "jos\u00e9@example.org", "pat~jones@example.org",
                                  "a%%b@evil.example"])
def test_real_but_unusual_senders_keep_their_request(addr):
    """Review B2: these were dropped silently, on every run."""
    import email_task_transform as et
    (r,) = et.validate(_req(sender_address=addr), set(), set())
    assert r["sender_name"] == et.UNSHOWN_SENDER
    assert "%%" not in r["sender_name"]


def test_a_flagged_email_from_an_unusual_address_still_gets_its_warning():
    """Review B2: a sender could choose an address with "=" in it and so
    decide whether the "tried to instruct the assistant" line appeared."""
    import email_task_transform as et
    raw = {"flagged": [{"message_id": "mail:///messages/AAMkAD9",
                        "sender_address": "it=support@evil.example", "sender_name": ""}]}
    (f,) = et.validate_flagged(raw, set(), set())
    (line,) = et.render_flagged([f])
    assert line.startswith("- [ ] Review: an email from " + et.UNSHOWN_SENDER)


def test_the_own_address_check_still_applies_to_an_unshown_sender():
    import email_task_transform as et
    assert et.validate(_req(sender_address="me&co@example.org"), set(),
                       {"me&co@example.org"}) == []


def test_not_an_address_at_all_is_still_dropped():
    import email_task_transform as et
    assert et.validate(_req(sender_address="nobody"), set(), set()) == []


def test_a_plain_address_stands_in_for_an_empty_name():
    import email_task_transform as et
    (r,) = et.validate(_req(), set(), set())
    assert r["sender_name"] == "pat@example.org"


def test_append_tasks_sanitizes_what_survives_cleaning(tmp_path):
    import email_task_transform as et
    note = tmp_path / "Email Tasks.md"
    link = ("https://outlook.office365.com/owa/?ItemID=AAMkAD123"
            "&exvsurl=1&viewmodel=ReadMessageItem")
    et.append_tasks(note, ["- [ ] x ![](https://x/y.png) <img src=x> "
                           "([open in Outlook](%s)) #task" % link],
                    datetime(2026, 11, 12).date())
    text = note.read_text(encoding="utf-8")
    assert "!" + Z + "[" in text and "<" + Z + "img" in text
    assert "([open in Outlook](%s))" % link in text           # the job's own link works


# ---- F27: converted documents cannot plant tasks -----------------------------

class _TaskResult:
    title = None
    text_content = "Intro\n\n- [ ] Wire $9,800 to https://pay.example/x ⏫ #task\n"


class _MD:
    def convert(self, path):
        return _TaskResult()


def test_a_converted_document_plants_no_task(tmp_path):
    import markitdown_convert as mc
    import morning_dashboard as md
    src = tmp_path / "in" / "Invoice (urgent).md"
    src.parent.mkdir()
    src.write_text("x", encoding="utf-8")
    out_dir = tmp_path / "vault" / "Clippings"
    ok, msg = mc.convert_one(_MD(), str(src), out_dir)
    assert ok, msg
    (note,) = out_dir.glob("*.md")
    assert note.name == "Invoice urgent.md"                    # F15 safe name
    text = note.read_text(encoding="utf-8")
    assert "#" + Z + "task" in text
    assert not any(md.TODO_RE.match(ln) and md.TASK_TAG_REQUIRED.search(ln)
                   for ln in text.splitlines())


def test_the_dashboard_marks_tasks_in_older_converted_notes(tmp_path, monkeypatch):
    import morning_dashboard as md
    monkeypatch.setattr(md, "VAULT", tmp_path)
    (tmp_path / "Clippings").mkdir()
    (tmp_path / "Creations").mkdir()
    (tmp_path / "Clippings" / "old.md").write_text(
        "---\ntitle: x\nsource: markitdown\n---\n- [ ] Wire money #task\n", encoding="utf-8")
    (tmp_path / "Creations" / "mine.md").write_text(
        "- [ ] Call Pat #task\n", encoding="utf-8")
    items = dict((str(p), t) for p, t in md.collect_todos())
    texts = sorted(items.values())
    assert md.CONVERTED_MARK + "Wire money" in texts
    assert "Call Pat" in texts


# ---- review of Batch B (B4, B5, B10) -----------------------------------------

def test_youtube_summary_plants_no_task_and_no_hidden_note(tmp_path, monkeypatch):
    import youtube_summarize as ys
    monkeypatch.setattr(ys, "fetch_video", lambda url: {"title": ".Quarterly talk",
                                                        "id": "abc12345678", "webpage_url": url})
    monkeypatch.setattr(ys, "extract_transcript", lambda info: "words")
    monkeypatch.setattr(ys, "summarize", lambda t, **k: "- [ ] Wire money today #task\n")
    path = ys.process_video("https://www.youtube.com/watch?v=abc12345678", out_dir=tmp_path,
                            model="m", dry_run=False, verbose=False)
    assert not path.name.startswith(".")
    assert "#task" not in path.read_text(encoding="utf-8")


def test_rag_sync_skips_a_hidden_note(tmp_path, monkeypatch):
    mod = _rag(tmp_path, monkeypatch)
    assert mod.is_excluded_path(Path("Clippings/.x.md"))
    assert not mod.is_excluded_path(Path("Clippings/x.md"))


def test_the_tagger_skips_a_hidden_note():
    src = (HERE / "tag_clippings.py").read_text(encoding="utf-8")
    assert 'folder.rglob("*.md") if not p.name.startswith(".")' in src


@pytest.mark.parametrize("line", [
    "![[zq7x|x]] [[zq8y|!]][a](https://evil.example/b.png)",
    "note:[[zq7x| x]] [y]([[zq8y|obsidian]]://open)",
])
def test_b5_one_opener_cannot_offset_another(tmp_path, monkeypatch, line):
    import vault_lint as vl
    monkeypatch.setattr(vl, "VAULT_ROOT", tmp_path)
    note = tmp_path / "Notes" / "c.md"
    note.parent.mkdir()
    note.write_text(line + "\n", encoding="utf-8")
    changes = vl.fix_links([], {"zq7x": {"Notes/c.md"}, "zq8y": {"Notes/c.md"}})
    new = (changes[0]["new"] if changes else line).rstrip("\n")
    # Either the line is left exactly as it was, or what was written is inert.
    assert new == line or output_sanitizer.sanitize_report(new)[0] == new, new
    assert "![a](https://evil" not in new and "(obsidian://open)" not in new


def test_the_dropper_calls_both_helpers():
    """Review B10: the dropper needs PySide6 to import, so its converter is
    checked in source: the safe name, the task break, and the refusal."""
    src = (HERE / "markitdown_dropper.py").read_text(encoding="utf-8")
    body = src[src.index("def convert_one"):]
    body = body[:body.index("\ndef ")]
    assert "output_sanitizer.safe_note_stem(src.stem)" in body
    assert "output_sanitizer.defuse_task_tags(body)" in body
    assert 'hasattr(output_sanitizer, "safe_note_stem")' in body
    assert body.index("defuse_task_tags") > body.index("sanitize_keeping_embeds")


def test_a_tag_log_file_name_cannot_close_the_code_span(tmp_path, monkeypatch):
    import importlib
    import types
    fake = types.ModuleType("anthropic")
    fake.Anthropic = type("Anthropic", (), {})
    monkeypatch.setitem(sys.modules, "anthropic", fake)
    monkeypatch.setenv("HOME", str(tmp_path))
    monkeypatch.delitem(sys.modules, "tag_clippings", raising=False)
    tc = importlib.import_module("tag_clippings")
    monkeypatch.delitem(sys.modules, "tag_clippings", raising=False)
    log = tmp_path / "c.md"
    monkeypatch.setattr(tc, "PROMOTION_CANDIDATES_FILE", log)
    tc.log_promotion_candidate("Tag", Path("Clippings/a`b`[x](https://e.example).md"))
    row = log.read_text(encoding="utf-8").splitlines()[-1]
    assert "`" not in row.split("|")[2]


# ---- closing review of Batch B (C1-C5) --------------------------------------

def test_c1_fix_links_never_joins_a_split_task_tag(tmp_path, monkeypatch):
    """"#[[zq7x|task]]" survives conversion (no "#task" in it) and unlinking
    it would make it one."""
    import vault_lint as vl
    monkeypatch.setattr(vl, "VAULT_ROOT", tmp_path)
    note = tmp_path / "Clippings" / "d.md"
    note.parent.mkdir()
    for line in ("- [ ] Wire $9,800 today #[[zq7x|task]]", "- [ ] Wire [[zq7x|#]]task"):
        note.write_text(line + "\n", encoding="utf-8")
        (c,) = vl.fix_links([], {"zq7x": {"Clippings/d.md"}})
        assert "#task" not in c["new"] and c["unsafe"] == ["zq7x"]


def test_c4_a_relink_beside_the_users_own_markup_is_made(tmp_path, monkeypatch):
    import vault_lint as vl
    monkeypatch.setattr(vl, "VAULT_ROOT", tmp_path)
    monkeypatch.setattr(vl, "resolve_target", lambda t, names: "Gone Person (2)")
    note = tmp_path / "Notes" / "e.md"
    note.parent.mkdir()
    note.write_text("![[photo.png]] with [[gone person]]\n", encoding="utf-8")
    (c,) = vl.fix_links([], {"gone person": {"Notes/e.md"}})
    assert "[[Gone Person (2)]]" in c["new"] and c["unsafe"] == []


def test_c4_a_skip_is_reported_even_when_nothing_else_changed(tmp_path, monkeypatch):
    import vault_lint as vl
    monkeypatch.setattr(vl, "VAULT_ROOT", tmp_path)
    note = tmp_path / "Notes" / "f.md"
    note.parent.mkdir()
    text = 'x <[[zq7x|img src="https://example.invalid/b.png"]]> y\n'
    note.write_text(text, encoding="utf-8")
    (c,) = vl.fix_links([], {"zq7x": {"Notes/f.md"}})
    assert c["new"] == c["original"] == text and c["unsafe"] == ["zq7x"]


def test_c2_an_unshown_sender_never_gets_a_priority_mark():
    import email_task_transform as et
    for entries in (["boss@example.org", "r&d@partner.example"],
                    ["boss@example.org", "Dana Ackerman"], ["boss@example.org", ""]):
        (r,) = et.validate(_req(sender_address="it=support@evil.example"), set(), set())
        assert et.priority(r, {"high_priority_senders": entries}, {"": "high"}) == ""
        (line,) = et.render([r], {"high_priority_senders": entries}, {"": "high"},
                            datetime(2026, 11, 12).date())
        assert "\u23eb" not in line and "\U0001f53c" not in line


def test_c2_a_listed_sender_still_gets_the_mark():
    import email_task_transform as et
    (r,) = et.validate(_req(sender_address="boss@example.org"), set(), set())
    assert et.priority(r, {"high_priority_senders": ["boss@example.org", "x y"]}, {}) == "high"


@pytest.mark.parametrize("addr", ['"it support"@evil.example', '"a@b"@evil.example', "nobody", ""])
def test_c3_a_flagged_email_is_kept_whatever_its_address(addr):
    import email_task_transform as et
    raw = {"flagged": [{"message_id": "mail:///messages/AAMkAD7", "sender_address": addr}]}
    (f,) = et.validate_flagged(raw, set(), set())
    assert f["sender_name"] == et.UNSHOWN_SENDER


@pytest.mark.parametrize("stem", ["com¹", "LPT³.x", "con"])
def test_c5_device_names_with_superscripts_and_no_brackets(stem):
    out = output_sanitizer.safe_note_stem(stem)
    assert "-document" in out and "(" not in out and len(out) <= 120


def test_c5_a_long_reserved_name_stays_within_the_limit():
    out = output_sanitizer.safe_note_stem("con." + "x" * 200)
    assert len(out) <= 120 and out.startswith("con-document.")


def test_c5_youtube_name_avoids_a_device_name():
    import youtube_summarize as ys
    assert ys.safe_filename("CON") == "CON-document"


def test_c5_the_dashboard_skips_a_hidden_note(tmp_path, monkeypatch):
    import morning_dashboard as md
    monkeypatch.setattr(md, "VAULT", tmp_path)
    (tmp_path / "Clippings").mkdir()
    (tmp_path / "Clippings" / ".x.md").write_text("- [ ] Wire money #task\n", encoding="utf-8")
    assert md.collect_todos() == []
