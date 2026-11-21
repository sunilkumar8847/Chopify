"""Subtitle-first transcripts: track choice, parsing, validation, refinement."""
import json
from pathlib import Path

import pytest

import download_and_transcribe as dt
import projects
import subtitles
import transcription


# ------------------------------------------------------------ track choice

def test_manual_track_in_the_spoken_language_wins():
    tracks = subtitles.track_list({
        "language": "hi",
        "subtitles": {"hi": [{}], "en": [{}], "live_chat": [{}]},
        "automatic_captions": {"hi-orig": [{}], "hi": [{}], "en": [{}], "fr": [{}]}})
    assert subtitles.choose_track(tracks) == ("hi", "manual")
    assert "fr" not in tracks["automatic"], "translations are not even kept"


def test_automatic_original_track_is_used_but_never_a_translation():
    assert subtitles.choose_track({"language": "hi", "manual": ["en"],
                                   "automatic": ["hi-orig", "hi"]}) == ("hi-orig", "automatic")
    # English speech: a Hindi auto track is a machine translation.
    key, reason = subtitles.choose_track({"language": "en", "manual": [],
                                          "automatic": ["hi", "hi-orig"]})
    assert key is None and "no en caption track" in reason
    key, reason = subtitles.choose_track({"language": None, "manual": [], "automatic": []})
    assert key is None and "unknown" in reason


def test_language_comes_from_the_orig_track_when_youtube_omits_it():
    tracks = subtitles.track_list({"automatic_captions": {"en-orig": [{}], "en": [{}], "de": [{}]}})
    assert tracks["language"] == "en"
    assert subtitles.choose_track(tracks) == ("en-orig", "automatic")


# ----------------------------------------------------------------- parsing

def json3(events):
    return {"events": events}


def test_json3_automatic_words_keep_youtube_word_timing():
    words = subtitles.parse_json3(json3([
        {"tStartMs": 1000, "dDurationMs": 3000, "segs": [
            {"utf8": "नमस्ते"}, {"utf8": " दोस्तों", "tOffsetMs": 600},
            {"utf8": " आज", "tOffsetMs": 1400}]},
        {"tStartMs": 2000, "dDurationMs": 10, "aAppend": 1, "segs": [{"utf8": "\n"}]},
        {"tStartMs": 4000, "dDurationMs": 1000, "segs": [{"utf8": "[संगीत]"}]},
    ]))
    assert [w["word"] for w in words] == [" नमस्ते", " दोस्तों", " आज"]
    assert [w["start"] for w in words] == [1.0, 1.6, 2.4]
    assert words[0]["end"] == 1.6 and words[-1]["end"] == 4.0


def test_json3_manual_cues_spread_words_over_the_cue():
    words = subtitles.parse_json3(json3([
        {"tStartMs": 0, "dDurationMs": 2000, "segs": [{"utf8": "hello big world"}]}]))
    assert [w["word"].strip() for w in words] == ["hello", "big", "world"]
    assert words[0]["start"] == 0 and words[-1]["end"] == pytest.approx(2.0, abs=0.01)
    assert all(a["end"] <= b["start"] for a, b in zip(words, words[1:]))


def test_vtt_and_srt_are_parsed_and_rolling_lines_are_not_repeated():
    vtt = ("WEBVTT\n\n00:00:01.000 --> 00:00:03.000\nhello there\n\n"
           "00:00:03.000 --> 00:00:05.000\nhello there\nhow are you\n")
    assert [w["word"].strip() for w in subtitles.parse_cues(vtt)] == [
        "hello", "there", "how", "are", "you"]
    srt = "1\n00:00:01,000 --> 00:00:02,000\n<i>Good</i> morning\n\n2\n01:00:00,000 --> 01:00:01,500\nlate\n"
    words = subtitles.parse_cues(srt)
    assert [w["word"].strip() for w in words] == ["Good", "morning", "late"]
    assert words[-1]["start"] == 3600.0


def test_segments_end_on_punctuation_pauses_and_length():
    words = [{"word": " a", "start": 0, "end": 0.5}, {"word": " b.", "start": 0.5, "end": 1},
             {"word": " c", "start": 1.1, "end": 1.5}, {"word": " d", "start": 5, "end": 5.5}]
    assert [s["text"] for s in subtitles.segments_from(words)] == [" a b.", " c", " d"]


# -------------------------------------------------------------- validation

def speech(n, gap=0.5, text="word{}"):
    return [{"word": " " + text.format(i), "start": i * gap, "end": i * gap + 0.4}
            for i in range(n)]


def test_good_track_passes_the_checks():
    stats = subtitles.check(speech(400), 220, "en")
    assert stats["words"] == 400 and stats["words_per_minute"] > 100


@pytest.mark.parametrize("words,duration,language,markers,reason", [
    (speech(20), 600, "en", 0, "empty"),
    (speech(100, gap=5), 3600, "en", 0, "too little speech"),
    (speech(200), 80, "en", 0, "different timeline"),
    (speech(300, text="la la la"), 200, "en", 0, "repeats"),
    (speech(200, text="नमस्ते{}"), 120, "en", 0, "not in Latin script"),
    (speech(200, text="مرحبا{}"), 120, "hi", 0, "unexpected script"),
    (speech(200), 120, "en", 400, "markers"),
])
def test_bad_tracks_are_refused_with_a_reason(words, duration, language, markers, reason):
    with pytest.raises(subtitles.SubtitleError) as exc:
        subtitles.check(words, duration, language, markers)
    assert reason in str(exc.value)


def test_compare_tolerates_spelling_variants_and_measures_offset():
    caption = [{"word": w, "start": i, "end": i + 0.5} for i, w in
               enumerate(["मैं", "बता", "रहा", "हूं", "ना", "घर"])]
    heard = [{"word": w, "start": i + 0.3, "end": i + 0.8} for i, w in
             enumerate(["मैं", "बता", "रहा", "हूँ", "ना,", "घर"])]
    similarity, offset = subtitles.compare(caption, heard)
    assert similarity > 0.9 and offset == pytest.approx(0.3)


def fake_heard(words, shift=0.0, language="hi", replace=None):
    def transcribe(start, end):
        out = [dict(w, start=w["start"] + shift, end=w["end"] + shift) for w in words
               if start <= w["start"] + shift < end]
        if replace:
            out = [dict(w, word=replace) for w in out]
        return out, language
    return transcribe


def test_verify_accepts_matching_captions():
    words = speech(600)
    windows = subtitles.probe_windows(words, 300)
    assert len(windows) == subtitles.PROBE_WINDOWS
    result = subtitles.verify(words, windows, fake_heard(words), "hi")
    assert result["verified"] and result["similarity"] > 0.9


def test_verify_refuses_wrong_text_shifted_timeline_and_wrong_language():
    words = [{"word": f" w{i % 97}x{i // 97}", "start": i * 0.5, "end": i * 0.5 + 0.4}
             for i in range(600)]
    windows = subtitles.probe_windows(words, 300)
    with pytest.raises(subtitles.SubtitleError, match="do not match"):
        subtitles.verify(words, windows, fake_heard(words, replace="other"), "hi")
    with pytest.raises(subtitles.SubtitleError, match="timeline"):
        subtitles.verify(words, windows, fake_heard(words, shift=3.0), "hi")
    with pytest.raises(subtitles.SubtitleError, match="speech is fr"):
        subtitles.verify(words, windows, fake_heard(words, language="fr"), "hi")
    # Hindi speech is sometimes detected as Urdu: same spoken language.
    assert subtitles.verify(words, windows, fake_heard(words, language="ur"), "hi")["verified"]


# ------------------------------------------------------------ refinement

def caption_data(tmp_path):
    media = tmp_path / "audio.webm"
    media.write_bytes(b"x")
    return {"video": str(media), "duration": 100.0, "transcript_source": "youtube_automatic",
            "words": [{"word": f" c{i}", "start": float(i), "end": i + 0.5} for i in range(100)],
            "segments": [{"start": 0.0, "end": 99.5, "text": "captions"}], "refined": []}


class FakeRuntime:
    def __init__(self):
        self.settings = transcription.plan({"profile": "balanced", "device": "cpu",
                                            "subtitles": "auto"})
        self.model = object()

    def fingerprint(self):
        return dt.fingerprint("medium", "float16", self.settings["decode"])


def test_refine_replaces_only_rendered_ranges_and_is_cached(tmp_path, monkeypatch):
    calls = []

    def fake_range(model, source, start, end, workdir, decode=None, name=""):
        calls.append((round(start, 1), round(end, 1)))
        words = [{"word": " whisper", "start": t + 0.1, "end": t + 0.4}
                 for t in range(int(start), int(end))]
        return [{"start": start, "end": end, "text": "whisper"}], words, "hi"
    monkeypatch.setattr(dt, "transcribe_range", fake_range)
    data = caption_data(tmp_path)
    saved = []
    data, n = transcription.refine(tmp_path, data, [(10, 20), (21, 30), (60, 70)],
                                   FakeRuntime(), saved.append)
    assert n == 2 and len(saved) == 2, "ranges 3 s apart are merged"
    words = data["words"]
    inside = [w["word"] for w in words if 10 <= w["start"] < 30]
    assert set(inside) == {" whisper"}
    assert [w["word"] for w in words if w["start"] < 10] == [f" c{i}" for i in range(10)]
    assert all(a["start"] <= b["start"] for a, b in zip(words, words[1:]))
    data, n = transcription.refine(tmp_path, data, [(12, 18), (60, 70)], FakeRuntime(),
                                   saved.append)
    assert n == 0 and len(calls) == 2, "refined ranges are never transcribed twice"


def test_manual_edits_are_locked_against_refinement(tmp_path, monkeypatch):
    monkeypatch.setattr(dt, "transcribe_range", lambda *a, **k: ([], [], "hi"))
    old = caption_data(tmp_path)
    new = json.loads(json.dumps(old))
    new["words"][15]["word"] = " edited"
    locked = transcription.lock_edits(old, new)
    lock = locked["refined"][-1]
    assert lock["by"] == "edit" and lock["start"] < 15 < lock["end"] < 16.5
    todo = transcription.missing_ranges(locked, [(10, 20)], FakeRuntime().fingerprint())
    assert all(not (a < 15.2 < b) for a, b in todo)
    assert todo and todo[0][0] == 10


def test_whisper_transcripts_are_never_refined(tmp_path):
    data = {**caption_data(tmp_path), "transcript_source": None}
    out, n = transcription.refine(tmp_path, data, [(0, 50)], FakeRuntime(), lambda d: None)
    assert n == 0 and out is data


# -------------------------------------------------------------- profiles

def test_saved_projects_keep_their_settings_and_cache_keys():
    legacy = projects.validate_options({"whisper_model": "medium", "device": "cpu"})
    settings = transcription.plan(legacy)
    assert settings["profile"] == "accurate" and settings["captions"] == "off"
    assert settings["decode"] == dt.LEGACY_DECODE
    assert transcription.cache_key_parts(settings) == [], "old transcript keys unchanged"


def test_new_projects_default_to_balanced_auto_gpu_and_captions(tmp_path):
    state = projects.create_project(tmp_path, "https://youtu.be/aaaaaaaaaaa", {"aspect": "9:16"})
    options = state["options"]
    assert (options["profile"], options["device"], options["subtitles"],
            options["whisper_model"]) == ("balanced", "auto", "auto", None)
    chosen = projects.create_project(tmp_path, "https://youtu.be/bbbbbbbbbbb",
                                     {"profile": "accurate", "device": "cpu",
                                      "subtitles": "off", "whisper_model": "small"})
    assert transcription.plan(chosen["options"])["model"] == "small"
    with pytest.raises(ValueError):
        projects.validate_options({"profile": "turbo"})


def test_auto_device_warns_on_cpu_fallback_and_explicit_cuda_fails(monkeypatch):
    monkeypatch.setattr(dt, "cuda_status", lambda: (False, "no NVIDIA GPU or CUDA driver was found"))
    device, compute, warning = dt.resolve_device("auto")
    assert (device, compute) == ("cpu", "int8") and "Transcribing on CPU" in warning
    with pytest.raises(RuntimeError, match="GPU transcription was requested"):
        dt.resolve_device("cuda")
    monkeypatch.setattr(dt, "cuda_status", lambda: (True, "ok"))
    assert dt.resolve_device("auto") == ("cuda", "float16", None)
    assert dt.resolve_device("cpu") == ("cpu", "int8", None)


def test_chunk_parts_are_reused_only_with_matching_settings():
    part = {"version": dt.TRANSCRIPT_VERSION, "index": 0, "start": 0.0, "end": 10.0,
            "identity": "x", "words": []}
    legacy = dt.fingerprint("medium", "float16")
    fast = dt.fingerprint("medium", "float16", {"beam": 1})
    assert dt._part_matches(part, 0, 0, 10, "x", legacy), "old parts stay valid"
    assert not dt._part_matches(part, 0, 0, 10, "x", fast)
    marked = {**part, "settings": fast}
    assert dt._part_matches(marked, 0, 0, 10, "x", fast)
    assert not dt._part_matches(marked, 0, 0, 10, "x", dt.fingerprint("small", "float16", {"beam": 1}))
    cpu = dt.fingerprint("medium", "int8", {"beam": 1})
    assert not dt._part_matches(marked, 0, 0, 10, "x", cpu), "int8 and float16 never mix"


def test_every_profile_uses_the_measured_safe_whisper_settings():
    # Small, beam 1 and batched decoding all lost Hindi speech in the
    # benchmark, so no profile may use them.
    for name in transcription.PROFILES:
        settings = transcription.plan({"profile": name, "subtitles": "auto"})
        assert settings["model"] == "medium" and settings["decode"] == dt.LEGACY_DECODE
    assert transcription.plan({"profile": "accurate", "subtitles": "auto"})["captions"] == "off"
    assert transcription.plan({"profile": "balanced", "subtitles": "auto"})["captions"] == "discover"
    assert transcription.plan({"profile": "fast", "subtitles": "auto"})["captions"] == "all"
    assert transcription.plan({"profile": "fast", "subtitles": "off"})["captions"] == "off"


def test_fast_keeps_youtube_word_timing_but_refines_manual_tracks(tmp_path):
    auto = {**caption_data(tmp_path), "captions": {"kind": "automatic"}}
    manual = {**caption_data(tmp_path), "captions": {"kind": "manual"}}
    fast = transcription.plan({"profile": "fast", "subtitles": "auto"})
    balanced = transcription.plan({"profile": "balanced", "subtitles": "auto"})
    assert not transcription.should_refine(auto, fast)
    assert transcription.should_refine(manual, fast), "manual cues have no word timing"
    assert transcription.should_refine(auto, balanced)
    assert not transcription.should_refine({"words": []}, balanced)
