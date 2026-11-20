"""Chunked, resumable transcription for long sources."""
import json
from pathlib import Path

import download_and_transcribe


class FakeResult:
    def __init__(self, stdout=""):
        self.stdout = stdout
        self.stderr = ""


class FakeWord:
    def __init__(self, word, start, end):
        self.word = word
        self.start = start
        self.end = end


class FakeSegment:
    def __init__(self, start, end, text, words=None):
        self.start = start
        self.end = end
        self.text = text
        self.words = words or []


class FakeInfo:
    def __init__(self, language="en", duration=1.0, probability=0.99):
        self.language = language
        self.duration = duration
        self.language_probability = probability


def test_merge_assigns_each_overlap_word_once():
    parts = [
        {"index": 0, "start": 0, "end": 1830, "language": "en",
         "segments": [{"start": 1790, "end": 1802, "text": "late"}],
         "words": [{"word": "late", "start": 1790, "end": 1800},
                   {"word": "edge", "start": 1805, "end": 1810}]},
        {"index": 1, "start": 1800, "end": 3600, "language": "en",
         "segments": [{"start": 1805, "end": 1810, "text": "edge"}],
         "words": [{"word": "edge", "start": 1805, "end": 1815},
                   {"word": "next", "start": 1830, "end": 1835}]},
    ]
    merged = download_and_transcribe.merge_transcript_parts(parts, 3600)
    assert [w["word"] for w in merged["words"]] == ["late", "edge", "next"]
    assert [s["text"] for s in merged["segments"]] == ["late"]
    assert merged["chunks"] == 2
    assert merged["duration"] == 3600


def test_chunked_transcription_resumes_completed_parts(tmp_path, monkeypatch):
    source = tmp_path / "audio.m4a"
    source.write_bytes(b"audio")
    calls = []
    monkeypatch.setattr(download_and_transcribe, "CHUNK_SECONDS", 1800)
    monkeypatch.setattr(download_and_transcribe, "OVERLAP_SECONDS", 30)
    monkeypatch.setattr(download_and_transcribe, "_probe_duration",
                        lambda path: 3600.0)
    monkeypatch.setattr(
        download_and_transcribe, "_extract_chunk",
        lambda source, start, end, path: Path(path).write_bytes(b"wav"))

    class Model:
        def transcribe(self, path, **kwargs):
            calls.append(path)
            return iter([FakeSegment(
                0, 1, "hello",
                [FakeWord("hello", 0.0, 1.0)])]), FakeInfo("en", 1.0)

    monkeypatch.setattr(download_and_transcribe, "_load_model",
                        lambda *args: Model())

    download_and_transcribe.transcribe_chunked(
        source, tmp_path, "tiny", "cpu", "int8", 3600.0)
    first = len(calls)
    download_and_transcribe.transcribe_chunked(
        source, tmp_path, "tiny", "cpu", "int8", 3600.0)
    assert len(calls) == first

    data = json.loads((tmp_path / "transcript.json").read_text(encoding="utf-8"))
    assert data["chunked"] is True
    assert data["chunks"] == 2
    assert data["duration"] == 3600.0
    assert data["transcript_version"] == download_and_transcribe.TRANSCRIPT_VERSION
    assert data["words"][0]["word"] == "hello"
    assert data["words"][0]["start"] == 0.0
    assert sorted(p.name for p in tmp_path.glob("transcript.part*.json")) == [
        "transcript.part000.json", "transcript.part001.json"]


def test_chunked_transcription_offsets_later_parts(tmp_path, monkeypatch):
    source = tmp_path / "audio.m4a"
    source.write_bytes(b"audio")
    monkeypatch.setattr(download_and_transcribe, "CHUNK_SECONDS", 1800)
    monkeypatch.setattr(download_and_transcribe, "_probe_duration",
                        lambda path: 3600.0)
    monkeypatch.setattr(
        download_and_transcribe, "_extract_chunk",
        lambda source, start, end, path: Path(path).write_bytes(b"wav"))

    class Model:
        def transcribe(self, path, **kwargs):
            return iter([FakeSegment(
                20, 21, "hello",
                [FakeWord("hello", 20.0, 21.0)])]), FakeInfo("en", 1.0)

    monkeypatch.setattr(download_and_transcribe, "_load_model",
                        lambda *args: Model())
    download_and_transcribe.transcribe_chunked(
        source, tmp_path, "tiny", "cpu", "int8", 3600.0)
    data = json.loads((tmp_path / "transcript.json").read_text(encoding="utf-8"))
    assert [w["start"] for w in data["words"]] == [20.0, 1820.0]


def test_transcribe_dispatches_to_chunked(tmp_path, monkeypatch):
    seen = {}
    monkeypatch.setattr(
        download_and_transcribe, "transcribe_chunked",
        lambda *args, **kwargs: seen.update(kwargs) or tmp_path / "transcript.json")
    download_and_transcribe.transcribe(
        tmp_path / "audio.m4a", tmp_path, "tiny", "cpu", "int8", 9000,
        chunked=True, source="https://example.com/v", media_kind="audio",
        render_from_sections=True, identity="abc", progress=lambda *a: None)
    assert seen["source"] == "https://example.com/v"
    assert seen["media_kind"] == "audio"
    assert seen["render_from_sections"] is True
    assert seen["identity"] == "abc"


def test_media_transcript_has_no_words(tmp_path):
    out = download_and_transcribe.write_media_transcript(
        tmp_path / "discovery.mp4", tmp_path, 5400,
        source="https://example.com/muted", identity="abc")
    data = json.loads(out.read_text(encoding="utf-8"))
    assert data["words"] == [] and data["segments"] == []
    assert data["render_from_sections"] is True
    assert data["media_kind"] == "video"
    assert data["duration"] == 5400
    assert data["source"] == "https://example.com/muted"


def test_chunked_transcription_merges_with_runtime_overlap(tmp_path, monkeypatch):
    source = tmp_path / "audio.m4a"
    source.write_bytes(b"audio")
    monkeypatch.setattr(download_and_transcribe, "CHUNK_SECONDS", 10)
    monkeypatch.setattr(download_and_transcribe, "OVERLAP_SECONDS", 4)
    monkeypatch.setattr(download_and_transcribe, "_probe_duration",
                        lambda path: 20.0)
    monkeypatch.setattr(
        download_and_transcribe, "_extract_chunk",
        lambda source, start, end, path: Path(path).write_bytes(b"wav"))

    class Model:
        def transcribe(self, path, **kwargs):
            return iter([FakeSegment(
                2, 3, "hello",
                [FakeWord("hello", 2.0, 3.0)])]), FakeInfo("en", 1.0)

    monkeypatch.setattr(download_and_transcribe, "_load_model",
                        lambda *args: Model())
    download_and_transcribe.transcribe_chunked(
        source, tmp_path, "tiny", "cpu", "int8", 20.0)
    data = json.loads((tmp_path / "transcript.json").read_text(encoding="utf-8"))
    assert [w["start"] for w in data["words"]] == [2.0, 12.0]


# --- GPU: pip-installed CUDA DLLs are found without touching global PATH ---

def _fake_venv(tmp_path, packages=("cublas", "cudnn")):
    for name in packages:
        (tmp_path / "Lib" / "site-packages" / "nvidia" / name / "bin").mkdir(parents=True)
    return tmp_path


def test_cuda_dll_dirs_lists_nvidia_wheel_bins(tmp_path):
    prefix = _fake_venv(tmp_path)
    found = download_and_transcribe.cuda_dll_dirs(prefix)
    assert [p.parent.name for p in found] == ["cublas", "cudnn"]
    assert download_and_transcribe.cuda_dll_dirs(tmp_path / "empty") == []


def test_enable_cuda_dlls_changes_only_the_given_process_env(tmp_path, monkeypatch):
    import os
    prefix = _fake_venv(tmp_path)
    added = []
    monkeypatch.setattr(download_and_transcribe.os, "name", "nt")
    monkeypatch.setattr(download_and_transcribe.os, "add_dll_directory",
                        lambda d: added.append(d), raising=False)
    env = {"PATH": r"C:\Windows"}
    dirs = download_and_transcribe.enable_cuda_dlls(prefix, env)
    assert len(dirs) == 2 and len(added) == 2
    assert env["PATH"].split(os.pathsep)[-1] == r"C:\Windows"
    assert env["PATH"].count("cublas") == 1
    download_and_transcribe.enable_cuda_dlls(prefix, env)    # idempotent
    assert env["PATH"].count("cublas") == 1


def test_enable_cuda_dlls_is_a_no_op_without_cuda_wheels(tmp_path):
    env = {"PATH": "x"}
    assert download_and_transcribe.enable_cuda_dlls(tmp_path, env) == []
    assert env == {"PATH": "x"}


def test_cpu_model_load_does_not_touch_cuda(monkeypatch):
    import sys, types
    calls = []
    monkeypatch.setattr(download_and_transcribe, "enable_cuda_dlls",
                        lambda *a, **k: calls.append("cuda"))
    fake = types.SimpleNamespace(WhisperModel=lambda *a, **k: ("model", a, k))
    monkeypatch.setitem(sys.modules, "faster_whisper", fake)
    download_and_transcribe._load_model("tiny", "cpu", "int8")
    assert calls == []
    download_and_transcribe._load_model("tiny", "cuda", "default")
    assert calls == ["cuda"]
