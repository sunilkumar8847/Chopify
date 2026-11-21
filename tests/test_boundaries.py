"""Phase 3: automatic clip duration and context preservation."""
import json

import pytest

import boundaries
import projects
import score_clips


def transcript(lines, start=0.0, wps=2.5, gap=0.4):
    """Word list from [(text, pause_before_seconds | None)], evenly timed."""
    words, t = [], start
    for item in lines:
        text, pause = (item, None) if isinstance(item, str) else item
        t += gap if pause is None else pause
        for token in text.split():
            words.append({"word": token, "start": round(t, 3),
                          "end": round(t + 1 / wps - 0.05, 3)})
            t += 1 / wps
    return words


def sentences_of(lines, **kwargs):
    return score_clips.sentenceize(transcript(lines, **kwargs))


def seg(sentences, first, last, rank=1, overall=8.5, hook="clip"):
    return {"start": sentences[first]["start"], "end": sentences[last]["end"],
            "duration": sentences[last]["end"] - sentences[first]["start"],
            "hook": hook, "overall": overall, "rank": rank}


def refine(segments, sentences, **kwargs):
    kwargs.setdefault("min_len", 1)
    kwargs.setdefault("max_len", 300)
    return boundaries.refine_segments(segments, sentences, **kwargs)


def covered(sentences, segment):
    return [s["text"] for s in sentences
            if s["start"] >= segment["start"] - 1e-6 and s["end"] <= segment["end"] + 1e-6]


STORY = [
    "My brother asked my dad for a new car last week.",
    "He said no, absolutely not, get a job first.",
    "So my brother got a job at the car dealership.",
    "Now he sells cars to my dad's friends every single day.",
]


# ------------------------------------------------------------ modes

def test_fixed_mode_changes_nothing():
    sents = sentences_of(STORY)
    original = [seg(sents, 1, 3)]
    result, stats = refine(original, sents, mode="fixed")
    assert result == original and stats["refined"] == 0
    assert "boundaries" not in result[0]


def test_unknown_mode_is_rejected():
    with pytest.raises(ValueError):
        refine([], [], mode="medium")


def test_no_transcript_leaves_clips_alone():
    original = [{"start": 10.0, "end": 40.0, "overall": 9, "rank": 1}]
    result, stats = refine(original, [])
    assert result == original and stats["unmatched"] == 1


# ------------------------------------------------------------ setup

def test_reference_opener_gets_its_setup():
    sents = sentences_of(STORY)
    result, stats = refine([seg(sents, 1, 3)], sents)
    assert covered(sents, result[0])[0] == STORY[0]
    info = result[0]["boundaries"]
    assert info["setup_sentences"] == 1 and stats["setup_added"] == 1
    assert info["original"] == [round(sents[1]["start"], 2), round(sents[3]["end"], 2)]
    assert any("reference" in note for note in info["notes"])


@pytest.mark.parametrize("lines", [
    # Hindi (Devanagari): "वो" refers back
    ["मेरे भाई ने पापा से नई कार माँगी।", "वो बोले, पहले नौकरी ढूँढो।",
     "फिर भाई ने कार शोरूम में नौकरी ले ली।"],
    # Hinglish: "Toh" continues the previous thought
    ["Bhai ne papa se nayi car maangi.", "Toh papa bole pehle job dhoondo.",
     "Aaj bhai unke dosto ko car bechta hai."],
    # Hindi connective "लेकिन"
    ["मैंने पाँच साल मेहनत की।", "लेकिन किसी ने भरोसा नहीं किया।",
     "आज मैं गोल्ड मेडल जीत गया।"],
])
def test_hindi_and_hinglish_openers_get_their_setup(lines):
    sents = sentences_of(lines)
    result, _ = refine([seg(sents, 1, 2)], sents)
    assert covered(sents, result[0])[0] == lines[0]


def test_reply_includes_the_question():
    lines = ["Can you guess what my dad said?", "Yeah, he said absolutely not.",
             "That was the best day of my life."]
    sents = sentences_of(lines)
    result, _ = refine([seg(sents, 1, 2)], sents)
    assert covered(sents, result[0])[0] == lines[0]


def test_reaction_opener_gets_what_it_reacts_to():
    lines = ["I just bought a tow truck for one dollar.", "You gotta be kidding.",
             "No, the auction had zero other bidders today."]
    sents = sentences_of(lines)
    result, _ = refine([seg(sents, 1, 2)], sents)
    assert covered(sents, result[0])[0] == lines[0]


@pytest.mark.parametrize("opener", [
    "This is the secret nobody tells you about saving money.",
    "There's a trick every chef knows about onions.",
    "Right now I'm running the biggest bakery in town.",
    "To be honest I never expected this to work at all.",
    "Everyone laughed when I said I'd quit my job.",
])
def test_standalone_openers_are_left_alone(opener):
    lines = ["Some earlier unrelated chatter about the weather today.", opener,
             "And it worked out better than anyone imagined."]
    sents = sentences_of(lines)
    result, _ = refine([seg(sents, 1, 2)], sents)
    assert covered(sents, result[0])[0] == opener
    assert result[0]["boundaries"]["setup_sentences"] == 0


def test_long_pause_marks_a_scene_change_and_stops_setup():
    lines = ["Completely different scene from before.",
             ("He walked in and the whole room went quiet.", 4.0),
             "Then everybody started clapping at once."]
    sents = sentences_of(lines)
    result, _ = refine([seg(sents, 1, 2)], sents)
    assert covered(sents, result[0])[0] == lines[1][0]
    assert any("long pause" in n for n in result[0]["boundaries"]["notes"])


def test_setup_is_capped():
    lines = ["Sentence number %d keeps going and going." % i for i in range(3)]
    lines += ["And then this happened.", "And then that.", "And then more.",
              "And then even more.", "And then the end.", "So it finished well."]
    sents = sentences_of(lines)
    last = len(sents) - 1
    result, _ = refine([seg(sents, last - 1, last)], sents)
    assert result[0]["boundaries"]["setup_sentences"] <= boundaries.MAX_SETUP_SENTENCES


# ------------------------------------------------------------ payoff

def test_unanswered_question_gets_its_answer():
    lines = ["I asked the chef one simple question.", "Why do you cry when cutting onions?",
             "Because onions release a gas that stings your eyes.",
             "Unrelated later sentence about something else entirely."]
    sents = sentences_of(lines)
    result, stats = refine([seg(sents, 0, 1)], sents)
    assert covered(sents, result[0])[-1] == lines[2]
    assert stats["payoff_added"] == 1


def test_short_reaction_after_the_punchline_is_kept():
    lines = ["And then the dog ate the whole wedding cake.", "No way!",
             ("Anyway, the next topic is about taxes and paperwork.", 3.0)]
    sents = sentences_of(lines)
    result, _ = refine([seg(sents, 0, 0)], sents)
    assert covered(sents, result[0]) == lines[:2]


def test_reaction_after_a_scene_change_is_not_kept():
    lines = ["And then the dog ate the whole wedding cake.", ("No way!", 3.0)]
    sents = sentences_of(lines)
    result, _ = refine([seg(sents, 0, 0)], sents)
    assert covered(sents, result[0]) == lines[:1]


def test_because_continuation_is_kept():
    lines = ["I quit my job the very next morning.",
             "Because nobody should work for a boss like that.",
             "Later I went to the market for vegetables."]
    sents = sentences_of(lines)
    result, _ = refine([seg(sents, 0, 0)], sents)
    assert covered(sents, result[0]) == lines[:2]


def test_hindi_question_gets_its_answer():
    lines = ["मैंने दादी से पूछा।", "आप गाँव क्यों छोड़ना नहीं चाहतीं?",
             "क्योंकि यहाँ मेरी पूरी ज़िंदगी की यादें हैं।"]
    sents = sentences_of(lines)
    result, _ = refine([seg(sents, 0, 1)], sents)
    assert covered(sents, result[0])[-1] == lines[2]


# ------------------------------------------------------------ trimming

def test_filler_start_and_outro_end_are_trimmed():
    lines = ["Um, uh.", "My grandmother learned video calling today at eighty.",
             "She called her sister and they cried for an hour.",
             "Subscribe and hit the bell icon."]
    sents = sentences_of(lines)
    result, stats = refine([seg(sents, 0, 3)], sents)
    assert covered(sents, result[0]) == lines[1:3]
    assert result[0]["boundaries"]["trimmed"] == 2 and stats["trimmed"] == 1


def test_trimming_never_goes_below_min_len():
    lines = ["Um, uh.", "Short point.", "Subscribe now."]
    sents = sentences_of(lines)
    original = seg(sents, 0, 2)
    result, _ = refine([original], sents, min_len=original["duration"])
    assert result[0]["boundaries"]["trimmed"] == 0


# ------------------------------------------------------------ duration

def test_too_long_clip_is_fitted_to_its_best_self_contained_part():
    lines = []
    for i in range(60):
        lines.append(f"Plain filler statement number {i} about nothing much here.")
    lines[20] = "Here's the secret nobody tells you: never pay full price."
    lines[21] = "I saved ten thousand dollars in one year doing this!"
    sents = sentences_of(lines)
    original = seg(sents, 0, len(sents) - 1)
    assert original["duration"] > 120 * boundaries.SOFT_MAX_FACTOR
    scorer = score_clips.WindowIndex(sents)
    result, stats = refine([original], sents, band=(30, 120),
                           score_window=lambda a, b: scorer.score(a, b)[1])
    clip = result[0]
    assert clip["end"] - clip["start"] <= 180 + 1
    assert stats["shortened"] == 1 and clip["boundaries"]["shortened"]
    assert boundaries.terminal({"text": covered(sents, clip)[-1]})


def test_verifier_can_veto_shortening():
    lines = [f"Statement number {i} with a little bit of content in it." for i in range(45)]
    sents = sentences_of(lines)
    original = seg(sents, 0, len(sents) - 1)
    calls = []
    result, stats = refine([original], sents, band=(30, 120),
                           verify_shorter=lambda s, a, b: calls.append((a, b)) or False)
    assert calls and stats["shortened"] == 0
    assert covered(sents, result[0])[0] == lines[0]
    assert any("AI judge" in n for n in result[0]["boundaries"]["notes"])


def test_preferred_length_sets_the_fit_target():
    lines = [f"Statement number {i} with a little bit of content in it." for i in range(30)]
    sents = sentences_of(lines)
    original = seg(sents, 0, len(sents) - 1)
    result, _ = refine([original], sents, preferred_len=30, band=(30, 120))
    assert result[0]["end"] - result[0]["start"] <= 30 * boundaries.SOFT_MAX_FACTOR + 1


@pytest.mark.parametrize("aspect,limit", [("9:16", 120), ("16:9", 200)])
def test_extensions_never_break_the_ceiling(aspect, limit):
    lines = [f"Background sentence {i} that sets up the scene slowly." for i in range(60)]
    lines.append("He finally said yes.")
    sents = sentences_of(lines)
    last = len(sents) - 1
    # Original clip already sits right at the limit.
    first = next(i for i in range(last, -1, -1)
                 if sents[last]["end"] - sents[i]["start"] > limit - 3) + 1
    result, _ = refine([seg(sents, first, last)], sents, max_len=200, aspect=aspect,
                       band=(60, 200))
    clip = result[0]
    assert clip["end"] - clip["start"] <= limit + 1e-6


def test_ceiling_values():
    assert boundaries.ceiling(300, "9:16") == 120
    assert boundaries.ceiling(120, "9:16") == 120
    assert boundaries.ceiling(300, "16:9") == 300


# ------------------------------------------------------------ neighbours & padding

def test_setup_never_runs_into_another_selected_clip():
    sents = sentences_of(STORY + ["He said it was the best decision ever."])
    first = seg(sents, 0, 0, rank=2, overall=8.1)
    second = seg(sents, 1, 2, rank=1, overall=9.0)   # wants sentence 0 as setup
    result, _ = refine([first, second], sents)
    a, b = sorted(result, key=lambda s: s["start"])
    assert a["end"] <= b["start"] + 1e-6
    assert any("no room" in n for n in result[1]["boundaries"]["notes"])


def test_padding_adds_air_without_touching_other_words():
    lines = ["The bakery opened at six in the morning.",
             "Everyone wanted the famous mango cake today.",
             "The owner baked forty cakes before noon.",
             "The market closed early because of rain."]
    sents = sentences_of(lines, gap=1.0)
    result, _ = refine([seg(sents, 2, 2)], sents)
    clip = result[0]
    first_word = sents[2]["words"][0]["start"]
    last_word = sents[2]["words"][-1]["end"]
    assert first_word - boundaries.LEAD_PAD - 1e-6 <= clip["start"] < first_word
    assert last_word < clip["end"] <= last_word + boundaries.TAIL_PAD + 1e-6
    assert clip["start"] >= sents[1]["end"]
    assert clip["end"] <= sents[3]["start"]


def test_padding_stays_inside_the_video():
    sents = sentences_of(["Hello there this is the very first line ever."], start=0.0, gap=0.05)
    result, _ = refine([seg(sents, 0, 0)], sents, duration=sents[0]["end"] + 0.1)
    assert result[0]["start"] >= 0 and result[0]["end"] <= sents[0]["end"] + 0.1


def test_hook_follows_the_new_opening_line():
    sents = sentences_of(STORY)
    result, _ = refine([seg(sents, 1, 3, hook="He said no")], sents,
                       make_hook=score_clips.make_hook)
    assert result[0]["hook"].lower().startswith("my brother")


def test_stats_report_before_and_after_durations():
    sents = sentences_of(STORY)
    _, stats = refine([seg(sents, 1, 3)], sents)
    assert stats["duration_before"][0] < stats["duration_after"][0]
    assert stats["version"] == boundaries.VERSION and stats["refined"] == 1


# ------------------------------------------------------------ select() integration

def write_project(tmp_path, lines, candidates, profile="talk"):
    words = transcript(lines)
    (tmp_path / "transcript.json").write_text(json.dumps(
        {"words": words, "duration": words[-1]["end"] + 1, "language": "en"}),
        encoding="utf-8")
    score_clips.save_candidates(tmp_path, candidates, {
        "source_duration": words[-1]["end"] + 1, "content_profile": profile})
    return score_clips.sentenceize(words)


def candidate(sents, first, last, overall=9.0):
    return {"start": sents[first]["start"], "end": sents[last]["end"],
            "duration": sents[last]["end"] - sents[first]["start"],
            "overall": overall, "raw_score": overall, "hook": "h",
            "reason": "r", "score_parts": {}}


def test_select_auto_mode_preserves_setup(tmp_path):
    sents = sentences_of(STORY)
    write_project(tmp_path, STORY, [candidate(sents, 1, 3)])
    segments, _ = score_clips.select(tmp_path, min_score=8.0, duration_mode="auto",
                                     min_len=1)
    stats = json.loads((tmp_path / "selection.json").read_text())
    assert covered(sents, segments[0])[0] == STORY[0]
    assert stats["boundaries"]["setup_added"] == 1
    saved = json.loads((tmp_path / "segments.json").read_text())
    assert saved[0]["boundaries"]["setup_sentences"] == 1


def test_select_fixed_mode_is_unchanged(tmp_path):
    sents = sentences_of(STORY)
    write_project(tmp_path, STORY, [candidate(sents, 1, 3)])
    segments, _ = score_clips.select(tmp_path, min_score=8.0)
    stats = json.loads((tmp_path / "selection.json").read_text())
    assert segments[0]["start"] == round(sents[1]["start"], 2)
    assert "boundaries" not in stats and "boundaries" not in segments[0]


def test_ai_judge_checks_shortened_clips(tmp_path):
    from tests.test_highlights import FakeProvider, scores
    lines = [f"Statement number {i} with a little bit of content in it." for i in range(60)]
    sents = sentences_of(lines)
    write_project(tmp_path, lines, [candidate(sents, 0, len(sents) - 1)],
                  profile="mixed")
    # The full clip (judged first) gets 9; the shorter version gets 3.
    answers = iter([9, 3, 3, 3])
    provider = FakeProvider(judge=lambda t: scores(next(answers)))
    segments, _ = score_clips.select(tmp_path, min_score=0.0, highlights=True,
                                     llm="test-model", judge_provider=provider,
                                     duration_mode="auto", min_len=1, max_len=300)
    stats = json.loads((tmp_path / "selection.json").read_text())
    assert stats["boundaries"]["shortened"] == 0
    assert any("AI judge" in n for n in segments[0]["boundaries"]["notes"])
    assert len(provider.calls) == 2  # the clip, then its shorter version


def test_ai_judge_approves_a_good_shorter_version(tmp_path):
    from tests.test_highlights import FakeProvider, scores
    lines = [f"Statement number {i} with a little bit of content in it." for i in range(60)]
    sents = sentences_of(lines)
    write_project(tmp_path, lines, [candidate(sents, 0, len(sents) - 1)],
                  profile="mixed")
    answers = iter([7, 8])
    provider = FakeProvider(judge=lambda t: scores(next(answers)))
    segments, _ = score_clips.select(tmp_path, min_score=0.0, highlights=True,
                                     llm="test-model", judge_provider=provider,
                                     duration_mode="auto", min_len=1, max_len=300)
    assert segments[0]["boundaries"]["shortened"]
    assert segments[0]["end"] - segments[0]["start"] <= 180 + 1


# ------------------------------------------------------------ options & CLI

def test_project_default_is_auto_and_validated():
    assert projects.validate_options({})["duration_mode"] == "auto"
    assert projects.validate_options({"duration_mode": None})["duration_mode"] == "auto"
    assert projects.validate_options({"duration_mode": "fixed"})["duration_mode"] == "fixed"
    with pytest.raises(ValueError):
        projects.validate_options({"duration_mode": "long"})


def test_duration_settings_change_selection_but_not_discovery():
    base = projects.validate_options({})
    d1, s1 = projects.score_keys("t", {"words": []}, base)
    d2, s2 = projects.score_keys("t", {"words": []}, {**base, "duration_mode": "fixed"})
    d3, s3 = projects.score_keys("t", {"words": []}, {**base, "aspect": "9:16"})
    assert d1 == d2 == d3 and len({s1, s2, s3}) == 3
    fixed = {**base, "duration_mode": "fixed"}
    _, f1 = projects.score_keys("t", {"words": []}, fixed)
    _, f2 = projects.score_keys("t", {"words": []}, {**fixed, "aspect": "9:16"})
    assert f1 == f2, "fixed mode ignores aspect, as before Phase 3"


def test_cli_duration_mode_flag_updates_an_existing_project(tmp_path, monkeypatch):
    import sys
    import chopify
    path = tmp_path / "proj"
    projects.init_project(path, "recording.mp4", {})
    monkeypatch.setattr(projects, "run_project", lambda *a, **k: {"status": "review"})
    monkeypatch.setattr(chopify, "_print_selection", lambda *a, **k: None)
    monkeypatch.setattr(sys, "argv", ["chopify.py", "--project", str(path),
                                      "--duration-mode", "fixed", "--no-input",
                                      "--no-preflight", "--action", "prepare"])
    chopify.main()
    assert projects.read_project(path)["options"]["duration_mode"] == "fixed"


# ------------------------------------------------------- strict 120 s limit

def test_a_clip_that_cannot_fit_is_rejected_not_cut():
    # One long unbroken thought: no sentence boundary gives a shorter clip.
    words = [{"word": f" w{i}", "start": i * 0.5, "end": i * 0.5 + 0.4} for i in range(400)]
    words[-1]["word"] = " end."
    sents = score_clips.sentenceize(words, gap=5)
    assert len(sents) == 1 and sents[0]["end"] - sents[0]["start"] > 120
    original = {"start": 0.0, "end": sents[0]["end"], "hook": "x", "overall": 9}
    result, _ = refine([original], sents, max_len=120, aspect="9:16", band=(30, 90))
    assert result[0]["too_long"] > 120
    kept, stats = score_clips.enforce_max_duration(result, {"passing": 1}, 120, "9:16")
    assert kept == [] and stats["rejected_too_long"][0]["length"] > 120
    assert stats["max_duration"] == 120 and stats["passing"] == 0


def test_enforced_limit_keeps_clips_within_it():
    segs = [{"start": 0, "end": 119.9}, {"start": 200, "end": 330}, {"start": 400, "end": 430}]
    kept, stats = score_clips.enforce_max_duration(segs, {}, 120, "9:16")
    assert [s["start"] for s in kept] == [0, 400]
    assert all(s["end"] - s["start"] <= 120 for s in kept)
    kept, _ = score_clips.enforce_max_duration(segs, {}, 300, "16:9")
    assert len(kept) == 3, "landscape keeps its own max_len"
