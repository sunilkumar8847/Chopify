"""Phase 2: shortlist, AI judge, combine, near-duplicates, cache, fallback.

A fake provider stands in for Ollama, so no model or network is needed.
"""
import json
import sys

import pytest

import highlights
import llm
import projects
import score_clips


# ---------------------------------------------------------------- helpers

VOCAB = ("river money camera coach launch secret penalty kitchen rocket "
         "violin desert market winter garden engine doctor island laptop "
         "festival tiger planet bridge piano soccer wallet thunder village "
         "museum battery sunset jungle pirate castle cricket monsoon").split()


def distinct_text(seed):
    """Different words per clip, like real transcripts (not a template)."""
    import random
    picks = random.Random(seed).sample(VOCAB, 10)
    return f"marker{seed} " + " ".join(picks)


def cand(start, overall, text=None, length=40.0):
    return {"start": float(start), "end": float(start) + length,
            "duration": length, "overall": float(overall),
            "raw_score": float(overall), "hook": f"clip at {start}",
            "text": text if text is not None else distinct_text(int(start)),
            "reason": "rule reason", "score_parts": {"hook": 0.5}}


def prompt_text(user_prompt):
    marker = "Clip transcript: "
    return user_prompt[user_prompt.index(marker) + len(marker):]


def scores(value, verdict="keep", reason="Strong punchline."):
    return {"hook": value, "payoff": value, "emotion": value, "standalone": value,
            "shareability": value, "verdict": verdict, "reason": reason}


class FakeProvider(llm.Provider):
    name = "fake"

    def __init__(self, judge=None, ok=True, replies=None, vectors=None):
        super().__init__("test-model")
        # Varied answers: identical scores for every clip count as degenerate.
        self.judge = judge or (lambda text: scores(5 + len(text) % 4))
        self.ok = ok
        self.replies = list(replies or [])
        self.vectors = vectors
        self.calls = []
        self.embed_calls = 0

    def available(self):
        return (True, "") if self.ok else (False, "Ollama is not running.")

    def chat_json(self, system, user, schema=None):
        self.calls.append({"system": system, "user": user, "schema": schema})
        if self.replies:
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return reply
        if system == highlights.TRANSLATE_PROMPT:
            reply = {"english": "EN: " + user.split("Transcript: ", 1)[1]}
            if "context_english" in schema["properties"]:
                reply["context_english"] = "EN context"
            return reply
        return {"summary": "A clip.", **self.judge(prompt_text(user))}

    def embed(self, texts, model=None):
        self.embed_calls += 1
        if self.vectors is None:
            raise llm.LLMError("no embedding model")
        return [self.vectors(text) for text in texts]


def run_stage(tmp_path, candidates, provider, duration=3600, **kwargs):
    transcript = {"words": [], "language": "hi", "duration": duration}
    return highlights.run(tmp_path, candidates, {"source_duration": duration},
                          transcript, provider=provider, log=lambda m: None,
                          **kwargs)


# --------------------------------------------------------------- shortlist

def test_shortlist_spreads_across_the_whole_video_and_keeps_the_best():
    # The strongest 100 candidates are all in the first 10 minutes.
    early = [cand(i * 6, 9.0 - i * 0.001) for i in range(100)]
    late = [cand(600 + i * 30, 5.0 + (i % 7) * 0.1) for i in range(100)]
    picked = highlights.shortlist(early + late, size=40, duration=3600)
    assert len(picked) == 40
    starts = [c["start"] for c in picked]
    assert sum(1 for s in starts if s >= 600) >= 15, "later parts are represented"
    assert {c["start"] for c in early[:20]} <= set(starts), "the best always get in"
    quarters = {min(3, int(s / 900)) for s in starts}
    assert quarters == {0, 1, 2, 3}
    assert starts == sorted(starts)


def test_shortlist_size_targets_30_to_60_clips():
    assert highlights.shortlist_size(10) == 10
    assert highlights.shortlist_size(100) == 40
    assert highlights.shortlist_size(450) == 45
    assert highlights.shortlist_size(5000) == 60


def test_quality_cap_never_allows_dozens_of_clips():
    assert highlights.quality_cap(11 * 60) == 3
    assert highlights.quality_cap(67 * 60) == 9
    assert highlights.quality_cap(6 * 3600) == 12
    assert highlights.quality_cap(6 * 3600, max_clips=25) == 25  # user decides


# ------------------------------------------------------------------- judge

def test_judge_scores_combine_with_rule_percentile(tmp_path):
    candidates = [cand(i * 100, 5 + i * 0.1) for i in range(10)]
    provider = FakeProvider(judge=lambda text: scores(9 if "marker900 " in text else 4))
    records, info = run_stage(tmp_path, candidates, provider)
    assert info["status"] == "ok" and info["judged"] == 10
    best = max(records, key=lambda r: r["overall"])
    assert best["start"] == 900
    judge = best["judge"]
    # final = 30% rule percentile + 70% AI score
    assert best["overall"] == pytest.approx(
        0.3 * judge["rule_percentile"] + 0.7 * judge["llm_score"], abs=0.01)
    assert judge["scores"]["hook"] == 9 and judge["verdict"] == "keep"
    assert best["reason"] == "Strong punchline."
    assert best["rule_reason"] == "rule reason"
    assert len(provider.calls) == 10  # one request per clip


def test_prompt_is_multilingual_structured_and_has_context(tmp_path):
    provider = FakeProvider()
    words = [{"word": "पहले", "start": 1.0, "end": 1.5},
             {"word": "वाली", "start": 1.5, "end": 2.0}]
    item = cand(10, 7, text="सुनो यार, asli baat ye hai।")
    highlights.run(tmp_path, [item], {"source_duration": 60},
                   {"words": words, "language": "hi"}, provider=provider,
                   log=lambda m: None)
    call = provider.calls[0]
    assert "Hinglish" in call["system"] and "Devanagari" in call["system"]
    assert "Detected language: hi" in call["user"]
    assert "सुनो यार, asli baat ye hai।" in call["user"]  # unescaped, readable
    assert "will NOT hear this): पहले वाली" in call["user"]
    required = call["schema"]["required"]
    assert required[:2] == ["summary", "reason"], "understand before scoring"
    assert set(highlights.RUBRIC_WEIGHTS) <= set(required)
    # Regression (prompt v1): a zero-filled example reply made qwen2.5:3b
    # copy all-zero scores for every real clip.
    assert '"hook":0' not in call["user"] + call["system"]


def test_invalid_entries_are_rejected_individually(tmp_path):
    candidates = [cand(i * 100, 6) for i in range(5)]
    bad = [scores(11),                              # out of range
           {**scores(7), "verdict": "great"},       # bad verdict
           {**scores(7), "hook": "high"},           # not a number
           {**scores(7), "hook": True}]             # bool is not a score
    # Clip 0 is valid; clips 1-4 get a bad answer twice (first try + retry).
    replies = [scores(8)] + [item for item in bad for _ in range(2)]
    provider = FakeProvider(replies=replies)
    records, info = run_stage(tmp_path, candidates, provider)
    assert info["judged"] == 1 and info["status"] == "partial"
    assert len(provider.calls) == 9
    judged = [r for r in records if "scores" in r["judge"]]
    assert [r["start"] for r in judged] == [0]
    unjudged = [r for r in records if "scores" not in r["judge"]]
    assert all(r["overall"] == 6 for r in unjudged), "rule score kept"


def test_bad_json_is_retried_once(tmp_path):
    candidates = [cand(0, 6)]
    provider = FakeProvider(replies=[llm.LLMError("not JSON"),
                                     {"clips": [{"id": 0, **scores(8)}]}])
    records, info = run_stage(tmp_path, candidates, provider)
    assert info["status"] == "ok" and len(provider.calls) == 2


def test_unusable_output_falls_back_to_rule_scores(tmp_path):
    candidates = [cand(i * 100, 5 + i) for i in range(3)]
    provider = FakeProvider(replies=[{"nonsense": True}] * 10)
    records, info = run_stage(tmp_path, candidates, provider)
    assert info["status"] == "invalid" and info["judged"] == 0
    assert sorted(r["overall"] for r in records) == [5, 6, 7]


def test_unavailable_model_falls_back_without_calls(tmp_path):
    candidates = [cand(i * 100, 5 + i) for i in range(3)]
    provider = FakeProvider(ok=False)
    records, info = run_stage(tmp_path, candidates, provider)
    assert info["status"] == "unavailable" and "not running" in info["reason"]
    assert provider.calls == []
    assert sorted(r["overall"] for r in records) == [5, 6, 7]


def test_no_model_means_rule_scores_with_quality_steps(tmp_path):
    records, info = highlights.run(
        tmp_path, [cand(0, 7), cand(100, 8)], {"source_duration": 600},
        {"words": []}, model=None, log=lambda m: None)
    assert info["status"] == "off" and info["judge"] is None
    assert [r["overall"] for r in records] == [7, 8]


def test_identical_answers_for_every_clip_are_rejected(tmp_path):
    # Real-model regression: prompt v1 made qwen2.5:3b answer all zeros/skip
    # for all 33 clips of a real video. That is not a judgment.
    candidates = [cand(i * 100, 5 + i * 0.3) for i in range(8)]
    provider = FakeProvider(judge=lambda text: scores(0, verdict="skip",
                                                      reason="Mid-thought."))
    records, info = run_stage(tmp_path, candidates, provider)
    assert info["status"] == "invalid" and info["judged"] == 0
    assert "identical" in info["reason"]
    assert sorted(r["overall"] for r in records) == sorted(
        c["overall"] for c in candidates), "rule scores kept"
    assert highlights.load_cache(tmp_path) == {}, "bad output is not cached"


def test_a_few_equal_answers_are_still_trusted(tmp_path):
    candidates = [cand(i * 100, 6) for i in range(3)]
    records, info = run_stage(tmp_path, candidates, FakeProvider())
    assert info["status"] == "ok" and info["judged"] == 3


def test_devanagari_clips_are_translated_then_judged(tmp_path):
    # Real-model finding: qwen2.5:3b judged Devanagari directly far worse
    # than the same content in English; translating first fixed it.
    provider = FakeProvider()
    words = [{"word": "पहले", "start": 1.0, "end": 1.5}]
    hindi = "मेरी दादी ने आज पहली बार वीडियो कॉल किया।"
    records, _ = highlights.run(tmp_path, [cand(10, 7, text=hindi)],
                                {"source_duration": 60},
                                {"words": words, "language": "hi"},
                                provider=provider, log=lambda m: None)
    translate, judge = provider.calls
    assert translate["system"] == highlights.TRANSLATE_PROMPT
    assert hindi in translate["user"] and "Context: पहले" in translate["user"]
    assert "Clip transcript: EN: " + hindi in judge["user"]
    assert "(the viewer will NOT hear this): EN context" in judge["user"]
    assert "translated from hi" in judge["user"]
    assert records[0]["judge"]["translation"] == "EN: " + hindi


@pytest.mark.parametrize("text", [
    "Bhai maine socha prank simple hoga, lekin papa wahi khade the!",
    "Nobody believed I could jump this gap. So I did it.",
    "Okay guys आज का video thoda alag hai, setup kar raha hoon abhi",
])
def test_english_and_hinglish_are_judged_directly(tmp_path, text):
    provider = FakeProvider()
    run_stage(tmp_path, [cand(0, 7, text=text)], provider)
    assert len(provider.calls) == 1
    assert provider.calls[0]["system"] == highlights.SYSTEM_PROMPT


def test_failed_translation_falls_back_to_the_original_text(tmp_path):
    hindi = "मैंने पाँच साल मेहनत की और आज गोल्ड मेडल जीता।"
    provider = FakeProvider(replies=[llm.LLMError("down"), {"wrong": 1}])
    records, info = run_stage(tmp_path, [cand(0, 7, text=hindi)], provider)
    assert info["status"] == "ok"
    assert "Clip transcript: " + hindi in provider.calls[-1]["user"]
    assert "translation" not in records[0]["judge"]


def test_devanagari_share():
    assert highlights.devanagari_share("नमस्ते दोस्तों") == 1.0
    assert highlights.devanagari_share("hello friends") == 0.0
    assert highlights.devanagari_share("") == 0.0


def test_summary_is_kept_for_review(tmp_path):
    records, _ = run_stage(tmp_path, [cand(0, 7)], FakeProvider())
    assert records[0]["judge"]["summary"] == "A clip."


def test_old_prompt_cache_entries_are_not_reused(tmp_path, monkeypatch):
    candidates = [cand(0, 6)]
    run_stage(tmp_path, candidates, FakeProvider())
    monkeypatch.setattr(highlights, "PROMPT_VERSION", highlights.PROMPT_VERSION + 1)
    again = FakeProvider()
    run_stage(tmp_path, candidates, again)
    assert len(again.calls) == 1


def test_skip_verdict_caps_the_score(tmp_path):
    provider = FakeProvider(judge=lambda text: scores(10, verdict="skip"))
    records, _ = run_stage(tmp_path, [cand(0, 9.5)], provider)
    assert records[0]["overall"] <= highlights.SKIP_CAP


# ------------------------------------------------------------------- cache

def test_judgments_are_cached_between_runs(tmp_path):
    candidates = [cand(i * 100, 6) for i in range(7)]
    first = FakeProvider()
    run_stage(tmp_path, candidates, first)
    assert len(first.calls) == 7
    second = FakeProvider()
    records, info = run_stage(tmp_path, candidates, second)
    assert second.calls == [] and info["from_cache"] == 7 and info["judged"] == 7
    # Edited transcript text is judged again; untouched clips stay cached.
    changed = [dict(candidates[0], text="a completely new punchline")] + candidates[1:]
    third = FakeProvider()
    _, info = run_stage(tmp_path, changed, third)
    assert len(third.calls) == 1 and info["from_cache"] == 6


def test_cache_is_per_model(tmp_path):
    candidates = [cand(0, 6)]
    run_stage(tmp_path, candidates, FakeProvider())
    other = FakeProvider()
    other.model = "other-model"
    run_stage(tmp_path, candidates, other)
    assert len(other.calls) == 1


# --------------------------------------------------------- near duplicates

HINDI_A = "मैंने अपनी पहली कंपनी में सब कुछ खो दिया और फिर से शुरू किया"
HINDI_A2 = "मैंने अपनी पहली कंपनी में सब कुछ खो दिया, फिर से शुरू किया था"
HINGLISH_A = "bhai maine apni pehli company mein sab kuch kho diya aur phir se start kiya"
HINGLISH_A2 = "bhai maine pehli company mein sab kuch kho diya tha aur phir start kiya"
ENGLISH_B = "The best camera settings for low light are a wide aperture and slow shutter"


@pytest.mark.parametrize("first,second", [(HINDI_A, HINDI_A2),
                                          (HINGLISH_A, HINGLISH_A2)])
def test_lexical_near_duplicates_in_hindi_and_hinglish(tmp_path, first, second):
    candidates = [cand(0, 7, first), cand(1000, 7, ENGLISH_B), cand(2000, 7, second)]
    provider = FakeProvider(judge=lambda text: scores(9 if text == first else 7))
    records, info = run_stage(tmp_path, candidates, provider)
    assert info["near_duplicates"] == 1 and info["dedupe_method"] == "lexical"
    assert [r["start"] for r in records] == [0, 1000], "the better twin is kept"


def test_different_topics_are_not_duplicates(tmp_path):
    texts = [HINDI_A, ENGLISH_B, "Today we test three budget microphones outdoors",
             "क्रिकेट मैच में आखिरी गेंद पर छक्का लगा"]
    records, info = run_stage(tmp_path, [cand(i * 500, 7, t) for i, t in enumerate(texts)],
                              FakeProvider())
    assert info["near_duplicates"] == 0 and len(records) == 4


CHATTER = [
    "No, you won't. Bro, I'm telling you this place is something, go. I don't "
    "know if I'll go anyplace else. Yo, I'm here for you bro, you know that",
    "You want to win in that game? You gotta look the part. Yeah. Maybe change "
    "the place before I give it to her. Thanks for the tip, I owe you one",
    "Okay, the problem is I'm not on a tow truck, but I'm assuming just take "
    "that, right? And then drive out of here. I mean, it is what it is",
    "It just feels like I'm actually, like, real life on the interstate or "
    "whatever, you know? Well, the problem is, like, going over the bridge",
]


def test_casual_english_chatter_is_not_a_false_duplicate(tmp_path):
    # Regression: real gameplay lines that scored 0.61-0.73 with plain
    # character n-grams were wrongly dropped as duplicates.
    candidates = [cand(i * 600, 7, text) for i, text in enumerate(CHATTER)]
    records, info = run_stage(tmp_path, candidates, FakeProvider())
    assert info["near_duplicates"] == 0 and len(records) == 4


def test_overlapping_clips_are_left_to_the_overlap_rule(tmp_path):
    same = "the exact same words spoken in a clip that overlaps another one"
    candidates = [cand(0, 7, same, length=60), cand(30, 7, same, length=60)]
    records, info = run_stage(tmp_path, candidates, FakeProvider())
    assert info["near_duplicates"] == 0


def test_two_clip_video_can_still_find_its_duplicate(tmp_path):
    candidates = [cand(0, 7, HINGLISH_A), cand(900, 7, HINGLISH_A2)]
    records, info = run_stage(tmp_path, candidates, FakeProvider())
    assert info["near_duplicates"] == 1


def test_embeddings_are_used_when_available(tmp_path):
    def vector(text):
        return [1.0, 0.0] if "story" in text else [0.0, 1.0]
    candidates = [cand(0, 7, "story one"), cand(900, 7, "story two"),
                  cand(1800, 7, "something else")]
    provider = FakeProvider(vectors=vector)
    records, info = run_stage(tmp_path, candidates, provider,
                              embed_model="nomic-embed-text")
    assert info["dedupe_method"] == "embeddings" and info["near_duplicates"] == 1
    assert provider.embed_calls == 1


# ---------------------------------------------------- select() integration

def write_workdir(tmp_path, candidates, duration=3600):
    score_clips.save_candidates(tmp_path, candidates, {
        "source_duration": duration, "seeds": len(candidates),
        "windows_scanned": len(candidates), "content_profile": "talk",
        "source_energy": 0.5})
    (tmp_path / "transcript.json").write_text(json.dumps(
        {"words": [], "duration": duration, "language": "en"}), encoding="utf-8")


def test_select_never_floods_with_clips(tmp_path):
    # 200 strong, non-overlapping candidates over an hour (varied rule scores).
    write_workdir(tmp_path, [cand(i * 18, 8.5 + (i * 37 % 100) / 100, length=15)
                             for i in range(200)])
    segments, mode = score_clips.select(tmp_path, min_score=8.0, highlights=True,
                                        llm="test-model",
                                        judge_provider=FakeProvider(
                                            judge=lambda t: scores(9 + len(t) % 2)))
    stats = json.loads((tmp_path / "selection.json").read_text())
    assert mode == "llm"
    assert 0 < len(segments) <= highlights.quality_cap(3600) == 8
    assert stats["highlights"]["shortlisted"] <= 60
    assert all("judge" in s and s["judge"]["verdict"] == "keep" for s in segments)


def test_select_with_judge_filters_weak_and_skip_clips(tmp_path):
    write_workdir(tmp_path, [cand(i * 300, 7.0) for i in range(6)])

    def judge(text):
        if "marker0 " in text:
            return scores(9.5)
        if "marker300 " in text:
            return scores(9.5, verdict="skip")
        return scores(3)

    segments, _ = score_clips.select(tmp_path, min_score=7.0, highlights=True,
                                     llm="test-model",
                                     judge_provider=FakeProvider(judge=judge))
    assert [s["start"] for s in segments] == [0]


def test_select_falls_back_when_the_judge_is_missing(tmp_path):
    write_workdir(tmp_path, [cand(i * 300, 8.5) for i in range(4)], duration=1200)
    segments, mode = score_clips.select(tmp_path, min_score=8.0, highlights=True,
                                        llm="qwen2.5:3b")  # tests disable LLMs
    stats = json.loads((tmp_path / "selection.json").read_text())
    assert mode == "heuristic"
    assert stats["highlights"]["status"] == "unavailable"
    assert len(segments) == 3  # 20-minute video -> cap 3, rule scores 8.5


def test_select_without_highlights_is_unchanged(tmp_path):
    write_workdir(tmp_path, [cand(i * 300, 8.5) for i in range(10)])
    segments, mode = score_clips.select(tmp_path, min_score=8.0)
    stats = json.loads((tmp_path / "selection.json").read_text())
    assert len(segments) == 10 and "highlights" not in stats and mode == "heuristic"


# ------------------------------------------------- Hindi / Hinglish rules

def words_from(text, start=0.0, step=0.4):
    out = []
    for i, token in enumerate(text.split()):
        out.append({"word": token, "start": start + i * step,
                    "end": start + i * step + 0.3})
    return out


def test_hindi_sentences_end_on_danda_and_become_candidates():
    sentence = ("मैंने अपनी पहली कंपनी में सब कुछ खो दिया और फिर से "
                "शुरू किया क्योंकि हार मानना विकल्प नहीं था।")
    words = []
    for n in range(8):
        words += words_from(sentence, start=n * 8.0)
    sentences = score_clips.sentenceize(words)
    assert len(sentences) == 8
    assert all(score_clips.is_arc_complete(s["text"]) for s in sentences)
    found, _ = score_clips.discover_candidates(sentences, min_len=15, max_len=60)
    assert found, "Hindi transcripts must produce clip candidates"


def test_devanagari_tokens_keep_vowel_signs():
    assert score_clips._tokens("मैंने काम किया।") == ["मैंने", "काम", "किया"]
    assert score_clips._tokens("Hello, World!") == ["hello", "world"]


def test_hinglish_and_hindi_hooks_are_recognized():
    plain = score_clips.sentence_features({"start": 0, "end": 3,
                                            "text": "ye baat hai yaar"})
    hooked = score_clips.sentence_features({"start": 0, "end": 3,
                                             "text": "suno yaar, asli baat ye hai"})
    hindi = score_clips.sentence_features({"start": 0, "end": 3,
                                            "text": "सुनो, असली बात ये है"})
    assert plain["hook"] == 0 and hooked["hook"] >= 2 and hindi["hook"] >= 2


def test_short_hinglish_cues_do_not_match_inside_english_words():
    feats = score_clips.sentence_features({"start": 0, "end": 3,
                                            "text": "The standard order of the dildo"})
    assert feats["emotion"] == 0


# ------------------------------------------------------- provider layer

def test_parse_json_tolerates_fences_and_prose():
    assert llm.parse_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert llm.parse_json('Sure! {"clips": []} Hope that helps') == {"clips": []}
    with pytest.raises(llm.LLMError):
        llm.parse_json("no json here")


def test_ollama_availability_explains_what_to_do(monkeypatch):
    monkeypatch.delenv("CHOPIFY_LLM_DISABLED")
    provider = llm.get_provider("ollama", "qwen2.5:3b")
    monkeypatch.setattr(provider, "_request", lambda *a, **k: {
        "models": [{"name": "llama3.2:3b"}]})
    ok, reason = provider.available()
    assert not ok and "ollama pull qwen2.5:3b" in reason

    def down(*args, **kwargs):
        raise llm.LLMError("connection refused")
    provider = llm.get_provider("ollama", "qwen2.5:3b")
    monkeypatch.setattr(provider, "_request", down)
    ok, reason = provider.available()
    assert not ok and "not running" in reason

    provider = llm.get_provider("ollama", "nomic-embed-text")
    monkeypatch.setattr(provider, "_request", lambda *a, **k: {
        "models": [{"name": "nomic-embed-text:latest"}]})
    assert provider.available() == (True, "")


def test_ollama_chat_sends_schema_and_parses_reply(monkeypatch):
    provider = llm.get_provider("ollama", "qwen2.5:3b")
    sent = {}

    def request(path, payload=None, timeout=None):
        sent.update(path=path, payload=payload)
        return {"message": {"content": '{"clips": []}'}}

    monkeypatch.setattr(provider, "_request", request)
    assert provider.chat_json("sys", "user", {"type": "object"}) == {"clips": []}
    assert sent["path"] == "/api/chat"
    assert sent["payload"]["format"] == {"type": "object"}
    assert sent["payload"]["model"] == "qwen2.5:3b"
    # Repeatable judgments: greedy decoding with a fixed seed.
    assert sent["payload"]["options"]["temperature"] == 0
    assert "seed" in sent["payload"]["options"]


def test_tests_never_reach_a_real_model():
    ok, reason = llm.get_provider("ollama", "qwen2.5:3b").available()
    assert not ok and "disabled" in reason


def test_unknown_provider_is_rejected():
    with pytest.raises(ValueError, match="Unknown LLM provider"):
        llm.get_provider("cloud-x", "m")


# ------------------------------------------------- options, keys and CLI

def test_project_defaults_enable_quality_first_selection():
    options = projects.validate_options({})
    assert options["highlights"] is True
    assert options["llm"] == "qwen2.5:3b" and options["llm_provider"] == "ollama"
    # Old saved projects (or CLI callers) without these keys get defaults.
    assert projects.validate_options({"highlights": None,
                                      "llm_provider": None})["highlights"] is True
    with pytest.raises(ValueError):
        projects.validate_options({"llm_provider": "cloud-x"})
    with pytest.raises(ValueError):
        projects.validate_options({"highlights": "yes"})


def test_judge_settings_change_selection_but_not_discovery():
    base = projects.validate_options({})
    d1, s1 = projects.score_keys("t", {"words": []}, base)
    d2, s2 = projects.score_keys("t", {"words": []}, {**base, "llm": "llama3.2:3b"})
    d3, s3 = projects.score_keys("t", {"words": []}, {**base, "highlights": False})
    assert d1 == d2 == d3
    assert len({s1, s2, s3}) == 3


def test_new_judge_prompt_reselects_existing_projects(monkeypatch):
    # Otherwise a project judged with a broken prompt keeps its old selection.
    base = projects.validate_options({})
    d1, s1 = projects.score_keys("t", {"words": []}, base)
    monkeypatch.setattr(highlights, "PROMPT_VERSION", highlights.PROMPT_VERSION + 1)
    d2, s2 = projects.score_keys("t", {"words": []}, base)
    assert d1 == d2 and s1 != s2


def test_cli_no_llm_and_no_highlights_update_an_existing_project(tmp_path, monkeypatch):
    import chopify
    path = tmp_path / "proj"
    projects.init_project(path, "recording.mp4", {})
    monkeypatch.setattr(chopify.sys, "argv", [
        "chopify.py", "--project", str(path), "--action", "prepare",
        "--no-input", "--no-preflight", "--no-llm", "--no-highlights"])
    monkeypatch.setattr(projects, "run_project", lambda *a, **k: {})
    chopify.main()
    options = projects.read_project(path)["options"]
    assert options["llm"] is None and options["highlights"] is False
