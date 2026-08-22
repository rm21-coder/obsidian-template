"""
test_podcast_transcribe.py — regression tests for the Apple Podcasts resolver.

Test classes
------------
- TestParseAppleUrl        show ID / episode ID extraction from share URLs
- TestItunesEpisodeTitle   show-scoped episode lookup
- TestResolveAppleUrl      which episode actually comes back

Why this file exists
--------------------
The resolver silently returned the wrong episode for every Apple Podcasts
URL. It looked an episode up by its `?i=` track ID alone, and Apple's lookup
API returns resultCount=0 for that (with or without entity/media/country), so
the title match had nothing to match on. The GUID match could not save it
either — feed GUIDs are UUIDs and never contain the track ID. Every URL fell
through to "newest in feed", which meant sharing a back-catalogue episode
transcribed the newest one instead, filed under the requested title, with
nothing in the vault to show it was the wrong audio.

Nothing failed, so nothing surfaced it. These tests pin both halves of the
fix: the show-scoped lookup that makes the title match work, and the refusal
to substitute a different episode when the requested one cannot be found.

Mocking strategy
----------------
podcast_transcribe.fetch_url is monkeypatched with a routing fake for every
test — the suite never makes a real request to iTunes or to a feed host.
"""
from __future__ import annotations

import json

import pytest

import podcast_transcribe as pt

SHOW_ID = "1528594034"
FEED_URL = "https://feeds.example.com/showfeed"

# Newest first, matching the order parse_rss returns items in.
EPISODES = [
    ("1000783297907", "Newest Episode", "uuid-aaaa-0001"),
    ("1000780446757", "Middle Episode", "uuid-bbbb-0002"),
    ("1000580800516", "The Very First Episode", "uuid-cccc-0003"),
]


def _feed_xml(items=None) -> bytes:
    items = EPISODES if items is None else items
    body = "".join(
        f"""<item>
              <title>{title}</title>
              <guid>{guid}</guid>
              <enclosure url="https://cdn.example.com/{track}.mp3" type="audio/mpeg"/>
            </item>"""
        for track, title, guid in items)
    return (f"""<?xml version="1.0"?>
        <rss version="2.0"><channel>
          <title>Example Show</title>{body}
        </channel></rss>""").encode("utf-8")


def make_fetch(*, episode_entries=None, feed_items=None, feed_url=FEED_URL):
    """Route fetch_url by URL: iTunes show lookup, iTunes episode list, feed.

    episode_entries=None means Apple returns the full episode list; pass []
    to model Apple knowing nothing about the show's episodes.
    """
    if episode_entries is None:
        episode_entries = [
            {"wrapperType": "podcastEpisode", "trackId": int(track),
             "trackName": title}
            for track, title, _guid in EPISODES
        ]

    def fetch_url(url, *, timeout=60):
        if "entity=podcastEpisode" in url:
            # Apple returns the show record first, then its episodes.
            results = [{"wrapperType": "track", "collectionName": "Example Show"}]
            results.extend(episode_entries)
            return json.dumps({"resultCount": len(results),
                               "results": results}).encode("utf-8")
        if "itunes.apple.com/lookup" in url:
            return json.dumps({"resultCount": 1, "results": [
                {"feedUrl": feed_url, "collectionName": "Example Show"}]
            }).encode("utf-8")
        if url == feed_url:
            return _feed_xml(feed_items)
        raise AssertionError(f"unexpected fetch_url({url!r})")

    return fetch_url


@pytest.fixture(autouse=True)
def _no_network(monkeypatch):
    """Fail loudly if a test forgets to install its own router."""
    def boom(url, *, timeout=60):
        raise AssertionError(f"un-mocked network call to {url!r}")
    monkeypatch.setattr(pt, "fetch_url", boom)


def _url(episode_id=None, show_id=SHOW_ID):
    base = f"https://podcasts.apple.com/us/podcast/some-show/id{show_id}"
    return f"{base}?i={episode_id}" if episode_id else base


class TestParseAppleUrl:
    def test_extracts_show_and_episode(self):
        assert pt.parse_apple_url(_url("1000580800516")) == (
            SHOW_ID, "1000580800516")

    def test_show_level_url_has_no_episode(self):
        assert pt.parse_apple_url(_url()) == (SHOW_ID, None)

    def test_episode_id_survives_extra_query_params(self):
        url = _url("1000580800516") + "&l=en-US"
        assert pt.parse_apple_url(url)[1] == "1000580800516"

    def test_url_without_show_id_raises(self):
        with pytest.raises(RuntimeError, match="could not parse show ID"):
            pt.parse_apple_url("https://podcasts.apple.com/us/podcast/nope")


class TestItunesEpisodeTitle:
    def test_finds_title_by_track_id(self, monkeypatch):
        monkeypatch.setattr(pt, "fetch_url", make_fetch())
        assert pt.itunes_episode_title(SHOW_ID, "1000580800516") == \
            "The Very First Episode"

    def test_scopes_the_lookup_to_the_show(self, monkeypatch):
        """The bug was looking up the episode ID on its own; the request must
        carry the show ID and ask for that show's episodes."""
        seen = []
        base = make_fetch()

        def spy(url, *, timeout=60):
            seen.append(url)
            return base(url, timeout=timeout)

        monkeypatch.setattr(pt, "fetch_url", spy)
        pt.itunes_episode_title(SHOW_ID, "1000580800516")
        assert f"id={SHOW_ID}" in seen[0]
        assert "entity=podcastEpisode" in seen[0]

    def test_unknown_episode_returns_empty(self, monkeypatch):
        monkeypatch.setattr(pt, "fetch_url", make_fetch())
        assert pt.itunes_episode_title(SHOW_ID, "9999999999") == ""

    def test_ignores_non_episode_records(self, monkeypatch):
        """The show record shares the payload and must not be mistaken for an
        episode even if its ID collides."""
        monkeypatch.setattr(pt, "fetch_url", make_fetch(episode_entries=[]))
        assert pt.itunes_episode_title(SHOW_ID, SHOW_ID) == ""


class TestResolveAppleUrl:
    def test_back_catalogue_episode_resolves_to_itself(self, monkeypatch):
        """The regression: this used to return the newest episode."""
        monkeypatch.setattr(pt, "fetch_url", make_fetch())
        mp3, title, show = pt.resolve_apple_podcasts_url(
            _url("1000580800516"), verbose=False)
        assert "The Very First Episode" in title
        assert mp3.endswith("1000580800516.mp3")
        assert show == "Example Show"

    def test_newest_episode_resolves_to_itself(self, monkeypatch):
        monkeypatch.setattr(pt, "fetch_url", make_fetch())
        mp3, title, _ = pt.resolve_apple_podcasts_url(
            _url("1000783297907"), verbose=False)
        assert "Newest Episode" in title
        assert mp3.endswith("1000783297907.mp3")

    def test_show_level_url_uses_newest(self, monkeypatch):
        monkeypatch.setattr(pt, "fetch_url", make_fetch())
        mp3, title, _ = pt.resolve_apple_podcasts_url(_url(), verbose=False)
        assert "Newest Episode" in title
        assert mp3.endswith("1000783297907.mp3")

    def test_unmatched_episode_raises_instead_of_substituting(self, monkeypatch):
        """An episode-specific URL that cannot be resolved must fail, not
        quietly hand back an hour of different audio."""
        monkeypatch.setattr(pt, "fetch_url", make_fetch(episode_entries=[]))
        with pytest.raises(RuntimeError, match="refusing to substitute"):
            pt.resolve_apple_podcasts_url(_url("1000580800516"), verbose=False)

    def test_falls_back_to_guid_when_apple_has_no_title(self, monkeypatch):
        """Feeds whose GUIDs do embed the track ID still resolve without a
        successful iTunes episode lookup."""
        items = [("1000783297907", "Newest Episode", "uuid-aaaa-0001"),
                 ("1000580800516", "The Very First Episode",
                  "tag:example.com,1000580800516")]
        monkeypatch.setattr(pt, "fetch_url",
                            make_fetch(episode_entries=[], feed_items=items))
        _mp3, title, _ = pt.resolve_apple_podcasts_url(
            _url("1000580800516"), verbose=False)
        assert "The Very First Episode" in title

    def test_title_match_tolerates_whitespace_and_case(self, monkeypatch):
        entries = [{"wrapperType": "podcastEpisode", "trackId": 1000580800516,
                    "trackName": "the very   FIRST episode"}]
        monkeypatch.setattr(pt, "fetch_url",
                            make_fetch(episode_entries=entries))
        _mp3, title, _ = pt.resolve_apple_podcasts_url(
            _url("1000580800516"), verbose=False)
        assert "The Very First Episode" in title

    def test_lookup_failure_does_not_substitute_newest(self, monkeypatch):
        """A transport error during the episode lookup is caught and logged;
        it must still not resolve to a different episode."""
        base = make_fetch()

        def flaky(url, *, timeout=60):
            if "entity=podcastEpisode" in url:
                raise RuntimeError("iTunes unreachable")
            return base(url, timeout=timeout)

        monkeypatch.setattr(pt, "fetch_url", flaky)
        with pytest.raises(RuntimeError, match="refusing to substitute"):
            pt.resolve_apple_podcasts_url(_url("1000580800516"), verbose=False)

    def test_feed_without_audio_items_raises(self, monkeypatch):
        monkeypatch.setattr(pt, "fetch_url", make_fetch(feed_items=[]))
        with pytest.raises(RuntimeError, match="no episodes with audio"):
            pt.resolve_apple_podcasts_url(_url("1000580800516"), verbose=False)


class TestTranscriptLayout:
    """Whisper ends segments on decoder boundaries, not sentence ones. Writing
    one line per segment produced prose chopped mid-clause; these pin the
    paragraph-per-marker layout that replaced it."""

    @staticmethod
    def _write(tmp_path, segments, **kw):
        return pt.write_transcript_md(
            out_dir=tmp_path, title="Show — Ep", source_url="https://e.test/x",
            result={"segments": segments, "language": "en", **kw},
            audio_seconds=90.0, model="test-model").read_text(encoding="utf-8")

    @staticmethod
    def _body(md):
        return md[md.index("## Transcript"):].splitlines()

    def test_segments_under_one_marker_join_into_a_paragraph(self, tmp_path):
        md = self._write(tmp_path, [
            {"start": 0.0, "text": "heard this from multiple"},
            {"start": 4.0, "text": "sources inside the company"},
            {"start": 9.0, "text": "and nobody denied it."}])
        assert ("heard this from multiple sources inside the company "
                "and nobody denied it.") in md

    def test_each_marker_starts_its_own_paragraph(self, tmp_path):
        lines = self._body(self._write(tmp_path, [
            {"start": 0.0, "text": "first block."},
            {"start": 31.0, "text": "second block."},
            {"start": 65.0, "text": "third block."}]))
        assert lines.count("") == len([l for l in lines if l.startswith("**[")])
        i = lines.index("**[0:31]**")
        assert lines[i - 1] == "", "a blank line must separate paragraphs"
        assert lines[i + 1] == "second block."

    def test_no_stray_blank_line_under_the_heading(self, tmp_path):
        lines = self._body(self._write(
            tmp_path, [{"start": 0.0, "text": "hello."}]))
        assert lines[:3] == ["## Transcript", "", "**[0:00]**"]

    def test_paragraphs_are_not_hard_wrapped(self, tmp_path):
        """The reader's width should decide wrapping, so a long block stays on
        one line however many segments built it."""
        segs = [{"start": float(i), "text": "word " * 20} for i in range(10)]
        body = [l for l in self._body(self._write(tmp_path, segs))
                if l and not l.startswith(("#", "**["))]
        assert len(body) == 1
        assert len(body[0]) > 500

    def test_empty_segments_are_skipped(self, tmp_path):
        md = self._write(tmp_path, [
            {"start": 0.0, "text": "kept."},
            {"start": 2.0, "text": "   "},
            {"start": 4.0, "text": ""},
            {"start": 6.0, "text": "also kept."}])
        assert "kept. also kept." in md

    def test_final_block_is_not_dropped(self, tmp_path):
        """The trailing block is flushed after the loop, not by the next
        marker — the easy off-by-one in a buffered writer."""
        md = self._write(tmp_path, [
            {"start": 0.0, "text": "opening."},
            {"start": 31.0, "text": "the very last thing said."}])
        assert "the very last thing said." in md

    def test_falls_back_to_raw_text_without_segments(self, tmp_path):
        md = self._write(tmp_path, [], text="a single blob of text")
        assert "a single blob of text" in self._body(md)

    def test_marker_waits_for_a_sentence_end(self, tmp_path):
        """The interval elapses mid-sentence; the break should slip to the
        next segment that follows a completed one."""
        lines = self._body(self._write(tmp_path, [
            {"start": 0.0, "text": "opening statement"},
            {"start": 31.0, "text": "still finishing this thought"},
            {"start": 34.0, "text": "which ends here."},
            {"start": 36.0, "text": "A clean new sentence."}]))
        assert "**[0:36]**" in lines
        assert "**[0:31]**" not in lines
        i = lines.index("**[0:36]**")
        assert lines[i + 1] == "A clean new sentence."
        assert "still finishing this thought which ends here." in lines[i - 2]

    def test_marker_gives_up_waiting_after_the_cap(self, tmp_path):
        """Speech that never punctuates must not swallow the whole episode."""
        segs = [{"start": 0.0, "text": "start."}]
        segs += [{"start": float(t), "text": "and on it goes"}
                 for t in range(20, 120, 10)]
        markers = [l for l in self._body(self._write(tmp_path, segs))
                   if l.startswith("**[")]
        assert len(markers) > 2, f"only got {markers}"

    def test_question_and_quote_endings_count_as_sentence_ends(self, tmp_path):
        for ending in ('is that so?', 'he said "yes."', 'stop!'):
            lines = self._body(self._write(tmp_path, [
                {"start": 0.0, "text": "opening"},
                {"start": 31.0, "text": "still going"},
                {"start": 33.0, "text": ending},
                {"start": 35.0, "text": "Next thought."}]))
            assert "**[0:35]**" in lines, f"{ending!r} not treated as an end"
