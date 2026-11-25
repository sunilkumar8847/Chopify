"""Story-based clip selection (clip mode "story") and the caption de-duplication fix."""
import json

import pytest

import projects
import story
import subtitles


class FakeJudge:
    """Returns scripted stories per chunk; counts calls."""
    label = "fake:judge"

    def __init__(self, stories_for):
        self.stories_for = stories_for
        self.calls = []

    def available(self):
        return True, ""

    def chat_json(self, system, user, schema=None):
        self.calls.append(user)
        first = int(user.split("Transcript sentences ")[1].split("-")[0])
        return {"summary": f"part starting at {first}", "stories": self.stories_for(first)}


def talk(seconds=900, gap_every=None):
    """Words every 0.4 s; a sentence ends every 10 words."""
    words, t, i = [], 0.0, 0
    while t < seconds:
        text = f"word{i}" + ("." if i % 10 == 9 else "")
        words.append({"word": text, "start": round(t, 2), "end": round(t + 0.3, 2)})
        t += 0.4
        i += 1
        if gap_every and i % gap_every == 0:
            t += 8.0                           # walking: no speech
    return words


def proposal(first, last, score=8, kind="funny", title="Zero dollars, boys"):
    return {"title": title, "type": kind, "first": first, "last": last, "hook_line": "hook",
            "why": "setup and payoff", "hook_score": score, "context_score": score,
            "middle_score": score, "payoff_score": score, "coherence_score": score,
            "engagement_score": score}


@pytest.fixture
def project(tmp_path):
    folder = tmp_path / "p"
    folder.mkdir()
    (folder / "transcript.json").write_text(json.dumps({"words": talk(), "duration": 900.0}),
                                            encoding="utf-8")
    return folder


def test_sentences_and_overlapping_chunks_cover_the_whole_video():
    sents = story.sentences(talk(900))
    assert all(s["end"] - s["start"] <= story.MAX_SENTENCE_SECONDS for s in sents)
    parts = story.chunks(sents)
    assert parts[0][0] == 0 and parts[-1][1] == len(sents) - 1
    for (a0, a1), (b0, b1) in zip(parts, parts[1:]):
        assert b0 <= a1, "chunks overlap so a story across a border is not lost"
        assert sents[a1]["end"] - sents[a0]["start"] <= story.CHUNK_SECONDS + 1


def test_duplicate_words_are_ignored():
    words = [{"word": " get", "start": 1.0, "end": 1.2}, {"word": " get", "start": 1.0, "end": 1.3},
             {"word": "burgers", "start": 1.3, "end": 1.8}]
    assert [w["word"] for w in story.clean_words(words)] == ["get", "burgers"]


def test_long_stories_keep_their_payoff_and_respect_the_shorts_limit():
    sents = story.sentences(talk(900))
    c = story.build({**proposal(0, 60), "chunk": [0, 60]}, sents, story.clean_words(talk(900)), [])
    assert c["end"] - c["start"] <= 120.0
    assert c["end"] == pytest.approx(sents[60]["end"] + 0.35, abs=0.01), "payoff kept"
    assert c["story"]["sentences"][1] == 60 and c["story"]["sentences"][0] > 0
    short = story.build({**proposal(5, 5), "chunk": [0, 60]}, sents,
                        story.clean_words(talk(900)), [])
    assert short["end"] - short["start"] >= 15, "extended with the following sentences"
    assert story.build({**proposal(5, 80), "chunk": [0, 60]}, sents, [], []) is None, \
        "only sentences the judge was shown"


def test_dead_air_lowers_the_score_and_break_screens_are_recorded():
    words = story.clean_words(talk(300, gap_every=12))
    sents = story.sentences(words)
    quiet = story.build({**proposal(0, 8), "chunk": [0, 50]}, sents, words, [])
    assert quiet["score_parts"]["dead_air"] > 0.3 and quiet["overall"] < quiet["raw_score"]
    talky = story.clean_words(talk(300))
    sents2 = story.sentences(talky)
    breaks = [{"start": 0, "end": 300, "label": "brb"}]
    brb = story.build({**proposal(0, 8), "chunk": [0, 50]}, sents2, talky, breaks)
    assert brb["score_parts"]["non_gameplay"] == 1.0, "recorded"
    assert brb["overall"] == brb["raw_score"], "observation only: scene labels never lower a score"


def test_overlapping_and_repeated_stories_are_removed():
    sents = story.sentences(talk(900))
    words = story.clean_words(talk(900))
    a = story.build({**proposal(10, 18, 9), "chunk": [0, 90]}, sents, words, [])
    b = story.build({**proposal(12, 20, 7), "chunk": [0, 90]}, sents, words, [])
    c = story.build({**proposal(60, 68, 6), "chunk": [0, 90]}, sents, words, [])
    kept, dropped = story.dedupe([b, c, a])
    assert [k["overall"] for k in kept] == [a["overall"], c["overall"]] and len(dropped) == 1


def test_selection_is_adaptive_not_a_fixed_threshold():
    def cand(score):
        return {"overall": score, "start": score, "end": score + 30}
    # GTA-like: best story 7.1, the old fixed minimum (8.0) would keep nothing.
    chosen, threshold = story.choose([cand(7.1), cand(6.0), cand(4.2)], 1800, min_score=8.0)
    assert threshold == pytest.approx(5.0) and [c["overall"] for c in chosen] == [7.1, 6.0]
    # A strong video keeps only stories close to its best.
    chosen, threshold = story.choose([cand(9.4), cand(8.8), cand(6.1)], 1800, min_score=8.0)
    assert threshold == pytest.approx(6.9) and [c["overall"] for c in chosen] == [9.4, 8.8]
    # Weak material: nothing below the absolute floor.
    chosen, _ = story.choose([cand(4.9), cand(3.0)], 1800)
    assert chosen == []
    # The length-based cap still applies.
    many = [cand(9 - i * 0.1) for i in range(30)]
    assert len(story.choose(many, 600)[0]) <= 4


def test_select_writes_valid_segments_and_caches_the_judge(project):
    judge = FakeJudge(lambda first: [proposal(first + 2, first + 9, 8),
                                     proposal(first + 3, first + 10, 6)])
    stats = story.select(project, judge, title="GTA 6", min_score=8.0)
    segments = json.loads((project / "segments.json").read_text(encoding="utf-8"))
    assert stats["mode"] == "story" and stats["passing"] == len(segments) >= 2
    assert stats["duplicates_removed"] >= 1
    checked = projects.validate_segments(segments, 900.0)
    assert all(15 <= s["end"] - s["start"] <= 120 for s in checked)
    assert all(s["story"]["type"] == "funny" and s["hook"] for s in checked)
    assert [s["start"] for s in segments] == sorted(s["start"] for s in segments)
    assert "What happened before this part: [0:00] part starting at 0" in judge.calls[1], \
        "later parts are judged with the story so far"
    calls = len(judge.calls)
    story.select(project, judge, title="GTA 6")
    assert len(judge.calls) == calls, "cached: selecting again costs no AI calls"


def test_judge_errors_are_reported_and_other_parts_still_count(project):
    import llm

    class Flaky(FakeJudge):
        def chat_json(self, system, user, schema=None):
            if "Transcript sentences 0-" in user:
                raise llm.LLMError("rate limit")
            return super().chat_json(system, user, schema)
    judge = Flaky(lambda first: [proposal(first + 2, first + 9, 8)])
    stats = story.judge_chunks(project, judge, story.sentences(talk()), "x",
                               sleep=lambda s: None)
    assert stats[1] and "not judged" in stats[1][0]
    assert stats[0], "the remaining parts still produced stories"


def test_project_pipeline_uses_story_mode(tmp_path, monkeypatch):
    video = tmp_path / "v.mp4"
    video.write_bytes(b"x")
    state = projects.create_project(tmp_path / "projects", str(video), {})
    assert state["options"]["clip_mode"] == "story", "new projects use story mode"
    path = projects.project_path(tmp_path / "projects", state["id"])
    calls = []

    def fake_select(folder, provider, **kwargs):
        calls.append(kwargs)
        (folder / "segments.json").write_text("[]", encoding="utf-8")
        (folder / "selection.json").write_text(json.dumps({"mode": "story"}), encoding="utf-8")
        return {}
    monkeypatch.setattr(story, "available", lambda options: FakeJudge(lambda f: []))
    monkeypatch.setattr(story, "select", fake_select)
    import score_clips
    monkeypatch.setattr(score_clips, "select", lambda *a, **k: calls.append("auto"))
    options = projects.validate_options({**state["options"], "min_score": 8.0})
    assert options["clip_mode"] == "story"
    with pytest.raises(ValueError):
        projects.validate_options({"clip_mode": "loud"})
    assert projects.validate_options({})["clip_mode"] == "auto", "older projects keep auto"
    key_story = projects.score_keys("k", {"words": []}, options)[1]
    key_auto = projects.score_keys("k", {"words": []}, {**options, "clip_mode": "auto"})[1]
    assert key_story != key_auto, "switching mode selects again"
    del path


# ---------------------------------------------------------- captions

def test_styled_caption_tracks_do_not_double_every_word():
    event = {"tStartMs": 167, "dDurationMs": 2567, "segs": [
        {"utf8": "​", "pPenId": 1}, {"utf8": "Finally, ", "pPenId": 3},
        {"utf8": "after ", "pPenId": 3, "tOffsetMs": 700}]}
    twin = json.loads(json.dumps(event))
    for seg in twin["segs"]:
        seg["pPenId"] = 4                       # same event drawn with another pen
    later = {"tStartMs": 3017, "dDurationMs": 1000, "segs": [{"utf8": "Finally again"}]}
    words = subtitles.parse_json3({"events": [event, twin, later]})
    assert [w["word"].strip() for w in words] == ["Finally,", "after", "Finally", "again"]
    assert subtitles.SUBTITLE_VERSION == 2, "caption transcripts are rebuilt once"


def test_out_of_range_scores_are_rejected_not_rescaled():
    """Seen with gpt-oss: sentence numbers written into the score fields."""
    sents = story.sentences(talk(300))
    words = story.clean_words(talk(300))
    p = {**proposal(20, 28), "chunk": [0, 50], "hook_score": 20, "payoff_score": 28}
    assert story.build(p, sents, words, []) is None
    ok = story.build({**proposal(0, 8, 7), "chunk": [0, 50], "type": "Heist!"}, sents, words, [])
    assert ok["score_parts"]["hook"] == 7
    assert ok["story"]["type"] == "story", "unknown story types are mapped, not rejected"


def test_groq_gets_low_reasoning_for_long_json_tasks():
    from ai.providers.groq import GroqProvider
    sent = []
    g = GroqProvider({"billing": "free_plan"}, key="k")
    g.send = lambda method, url, headers, body, timeout: (
        sent.append(json.loads(body)) or (200, {}, json.dumps(
            {"choices": [{"message": {"content": "{\"summary\": \"s\", \"stories\": []}"}}],
             "usage": {}}).encode()))
    g.generate("openai/gpt-oss-20b", "sys", "user", schema=story.SCHEMA, reasoning="low",
               max_output_tokens=4000)
    assert sent[0]["reasoning_effort"] == "low" and sent[0]["max_completion_tokens"] == 4000
    assert sent[0]["response_format"] == {"type": "json_object"}, "not strict"
    g.generate("openai/gpt-oss-20b", "sys", "user", schema={"type": "object"})
    assert "reasoning_effort" not in sent[1], "other callers unchanged"


def test_a_failed_part_does_not_invalidate_the_cache_of_later_parts(project):
    import llm
    fail = {"once": True}

    class FlakyOnce(FakeJudge):
        def chat_json(self, system, user, schema=None):
            if "Transcript sentences 0-" in user and fail["once"]:
                fail["once"] = False
                raise llm.LLMError("rate limit")
            return super().chat_json(system, user, schema)
    judge = FlakyOnce(lambda first: [proposal(first + 2, first + 9, 8)])
    sents = story.sentences(talk())
    story.judge_chunks(project, judge, sents, "x", sleep=lambda s: None)
    calls = len(judge.calls)
    story.judge_chunks(project, judge, sents, "x", sleep=lambda s: None)
    assert len(judge.calls) == calls, "second pass: every part from the cache"
