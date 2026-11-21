"""Subtitle-first prepare, Whisper fallback, device reporting and caching."""
import json

import pytest

import download_and_transcribe as dt
import projects
import subtitles

URL = "https://youtu.be/aaaaaaaaaaa"


def meta(**extra):
    return {"source": URL, "url": True, "duration": 600.0, "width": 1920, "height": 1080,
            "has_audio": True, "has_video": True, "estimated_size": 12 * 1024**3,
            "title": "Test", "live_status": "not_live",
            "captions": {"language": "hi", "manual": [], "automatic": ["hi-orig"]}, **extra}


def caption_events(n=900, gap=0.6):
    return {"events": [{"tStartMs": int(i * gap * 1000), "dDurationMs": 500,
                        "segs": [{"utf8": f"शब्द{i % 113}"}]} for i in range(n)]}


@pytest.fixture
def pipeline(tmp_path, monkeypatch):
    """A long-URL project whose network, Whisper and scoring are fakes."""
    calls = {"whisper": 0, "windows": [], "fetch": 0, "discover": 0, "select": 0}
    audio = tmp_path / "audio.webm"
    audio.write_bytes(b"audio")
    monkeypatch.setattr(projects.ingest, "probe_source",
                        lambda value: meta() if value == URL else {"duration": 600.0})
    monkeypatch.setattr(projects.ingest, "is_long_source", lambda m: True)
    monkeypatch.setattr(projects.ingest, "check_disk", lambda *a: None)
    monkeypatch.setattr(projects.ingest, "download_discovery",
                        lambda url, workdir, meta=None: (audio, "audio"))
    monkeypatch.setattr(dt, "cuda_status", lambda: (True, "NVIDIA GPU with float16"))
    monkeypatch.setattr(dt, "_load_model", lambda *a: object())
    behaviour = {"events": caption_events(), "heard_shift": 0.0}

    def fetch(url, workdir, key, kind, runner=None):
        calls["fetch"] += 1
        out = workdir / "captions.hi-orig.json3"
        out.write_text(json.dumps(behaviour["events"]), encoding="utf-8")
        return out
    monkeypatch.setattr(subtitles, "fetch", fetch)
    words = subtitles.parse_json3(caption_events())

    def transcribe_range(model, source, start, end, workdir, decode=None, name=""):
        calls["windows"].append((name, start, end))
        shift = behaviour["heard_shift"]
        heard = [dict(w, word=w["word"] if not behaviour.get("garbage") else " xyz",
                      start=w["start"] + shift, end=w["end"] + shift)
                 for w in words if start <= w["start"] + shift < end]
        return [{"start": start, "end": end, "text": "x"}], heard, "hi"
    monkeypatch.setattr(dt, "transcribe_range", transcribe_range)

    def whisper(video, workdir, *args, **kwargs):
        calls["whisper"] += 1
        calls["whisper_args"] = (args, kwargs)
        projects.atomic_json(workdir / "transcript.json", {
            "video": str(video), "source": URL, "duration": 600.0,
            "words": [{"word": " hi", "start": 0, "end": 1}], "segments": []})
    monkeypatch.setattr(projects.dt, "transcribe", whisper)

    def discover(path, **kwargs):
        calls["discover"] += 1
        projects.atomic_json(path / "candidates.json", {"candidates": []})

    def select(path, **kwargs):
        calls["select"] += 1
        projects.atomic_json(path / "segments.json",
                             [{"start": 100.0, "end": 130.0, "hook": "h", "overall": 9}])
        projects.atomic_json(path / "selection.json", {"passing": 1})
    monkeypatch.setattr(projects.score_clips, "discover", discover)
    monkeypatch.setattr(projects.score_clips, "select", select)
    state = projects.create_project(tmp_path / "projects", URL, {})
    path = projects.project_path(tmp_path / "projects", state["id"])
    return path, calls, behaviour


def test_good_captions_replace_whole_video_whisper(pipeline):
    path, calls, _ = pipeline
    projects.run_project(path, action="prepare")
    data = projects.read_json(path / "transcript.json")
    assert calls["whisper"] == 0, "no whole-video Whisper"
    assert data["transcript_source"] == "youtube_automatic"
    assert data["captions"]["track"] == "hi-orig" and data["captions"]["verification"]["verified"]
    assert len(calls["windows"]) == subtitles.PROBE_WINDOWS, "only short check windows"
    assert sum(e - s for _, s, e in calls["windows"]) <= 4 * subtitles.PROBE_SECONDS
    state = projects.read_project(path)
    info = state["transcription"]
    assert (info["device"], info["compute"], info["source"]) == ("cuda", "float16",
                                                                 "youtube_automatic")
    assert info["warning"] is None and state["status"] == "review"


@pytest.mark.parametrize("problem,expect", [
    ("shift", "timeline"), ("garbage", "do not match"), ("empty", "empty")])
def test_bad_captions_fall_back_to_whisper_with_a_visible_reason(pipeline, problem, expect):
    path, calls, behaviour = pipeline
    if problem == "shift":
        behaviour["heard_shift"] = 4.0
    elif problem == "garbage":
        behaviour["garbage"] = True
    else:
        behaviour["events"] = {"events": []}
    projects.run_project(path, action="prepare")
    assert calls["whisper"] == 1, "Whisper fallback ran"
    state = projects.read_project(path)
    assert expect in state["transcription"]["captions_refused"]
    assert state["transcription"]["source"] == "whisper"
    args, kwargs = calls["whisper_args"]
    assert args[:3] == ("medium", "cuda", "float16")
    assert kwargs["decode"] == dt.LEGACY_DECODE, "fallback never uses lossy settings"
    assert "transcript_source" not in projects.read_json(path / "transcript.json")


def test_cpu_fallback_is_shown_never_silent(pipeline, monkeypatch):
    path, calls, _ = pipeline
    monkeypatch.setattr(dt, "cuda_status", lambda: (False, "no NVIDIA GPU or CUDA driver was found"))
    projects.run_project(path, action="prepare")
    info = projects.read_project(path)["transcription"]
    assert info["device"] == "cpu" and "Transcribing on CPU" in info["warning"]
    assert info["requested_device"] == "auto"


def test_explicit_gpu_that_cannot_work_fails_clearly(pipeline, monkeypatch):
    path, calls, _ = pipeline
    projects.update_project(path, lambda s: s["options"].update(device="cuda"))
    monkeypatch.setattr(dt, "cuda_status", lambda: (False, "no NVIDIA GPU or CUDA driver was found"))
    with pytest.raises(RuntimeError, match="GPU transcription was requested"):
        projects.run_project(path, action="prepare")
    assert projects.read_project(path)["status"] == "failed"


def test_style_metadata_and_selection_changes_never_transcribe_again(pipeline):
    path, calls, _ = pipeline
    projects.run_project(path, action="prepare")
    first = (calls["fetch"], len(calls["windows"]), calls["whisper"], calls["discover"])
    projects.update_project(path, lambda s: s["options"].update(style="bold", caption=None))
    projects.run_project(path, action="prepare")
    projects.update_project(path, lambda s: s["options"].update(min_score=6.0))
    projects.run_project(path, action="prepare")
    assert (calls["fetch"], len(calls["windows"]), calls["whisper"]) == first[:3]
    assert calls["discover"] == first[3], "discovery cached too"
    assert calls["select"] == 2, "only clip selection re-ran"


def test_refined_words_do_not_invalidate_discovery(pipeline):
    path, calls, _ = pipeline
    projects.run_project(path, action="prepare")
    state = projects.read_project(path)
    data = projects.read_json(path / "transcript.json")
    before = projects.score_keys(state["cache"]["transcript"], data, state["options"])
    data["words"] = data["words"][:10]
    data["refined"].append({"start": 1, "end": 2, "settings": {}})
    assert projects.score_keys(state["cache"]["transcript"], data, state["options"]) == before
    data["refined"].append({"start": 3, "end": 4, "by": "edit"})
    assert projects.score_keys(state["cache"]["transcript"], data, state["options"]) != before


def test_captions_off_uses_whisper_for_the_whole_video(pipeline):
    path, calls, _ = pipeline
    projects.update_project(path, lambda s: s["options"].update(subtitles="off", profile="accurate"))
    projects.run_project(path, action="prepare")
    assert calls["fetch"] == 0 and calls["whisper"] == 1
    assert calls["whisper_args"][1]["decode"] == dt.LEGACY_DECODE


def test_interrupted_chunk_resumes_from_its_checkpoint(tmp_path, monkeypatch):
    source = tmp_path / "audio.m4a"
    source.write_bytes(b"audio")
    monkeypatch.setattr(dt, "CHECKPOINT_SECONDS", 100)
    monkeypatch.setattr(dt, "_probe_duration", lambda p: 1000.0)
    extracted = []
    monkeypatch.setattr(dt, "_extract_chunk",
                        lambda src, start, end, p: extracted.append(start) or p.write_bytes(b"w"))

    class Seg:
        def __init__(self, t):
            self.start, self.end, self.text = t, t + 1, f"s{t}"
            self.words = [type("W", (), {"word": f"w{t}", "start": t, "end": t + 1})()]

    class Info:
        language, duration, language_probability = "hi", 1000.0, 1.0

    class Crashing:
        def transcribe(self, path, **kw):
            def gen():
                for t in range(0, 1000, 50):
                    if t == 450:
                        raise KeyboardInterrupt("power cut")
                    yield Seg(t)
            return gen(), Info()

    with pytest.raises(KeyboardInterrupt):
        dt._transcribe_part(Crashing(), source, tmp_path, tmp_path, 0, 0.0, 1000.0,
                            "id", dt.fingerprint("medium", "float16"), None, True)
    saved = json.loads((tmp_path / "transcript.part000.partial.json").read_text(encoding="utf-8"))
    assert saved["covered"] == 401.0, "last checkpoint before the crash"

    class Rest:
        def transcribe(self, path, **kw):
            return iter([Seg(t) for t in range(0, 600, 50) if t >= 1]), Info()
    part = dt._transcribe_part(Rest(), source, tmp_path, tmp_path, 0, 0.0, 1000.0,
                               "id", dt.fingerprint("medium", "float16"), None, True)
    assert extracted == [0.0, 401.0], "second run only extracts the remainder"
    starts = [w["start"] for w in part["words"]]
    assert starts[:9] == [0, 50, 100, 150, 200, 250, 300, 350, 400]
    assert starts[9] == 451.0 and starts == sorted(starts)
