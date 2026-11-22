"""Benchmark framework: metrics, unavailable routes, read-only projects."""
import json
import subprocess

import pytest

from ai import benchmark
from ai.base import (REAL, SYNTHETIC, Generation, PrivacyBlocked, Segment, Transcription,
                     Usage, Word)


def test_metrics():
    assert benchmark.wer(["a", "b", "c", "d"], ["a", "x", "c", "d"]) == 0.25
    assert benchmark.wer(["hello", "world"], ["Hello,", "world!"]) == 0.0, "punctuation/case"
    assert benchmark.cer(["abcd"], ["abxd"]) == 0.25
    ref = [{"word": "a", "start": 1.0}, {"word": "b", "start": 2.0}, {"word": "c", "start": 3.0}]
    hyp = [{"word": "a", "start": 1.1}, {"word": "b", "start": 2.3}, {"word": "c", "start": 3.0}]
    assert benchmark.timing_offset(ref, hyp) == (0.1, 3)
    assert benchmark.script_share(["नमस्ते", "bro"])["devanagari"] > 0.5
    assert benchmark.spearman([1, 2, 3, 4], [10, 20, 30, 40]) == 1.0
    assert benchmark.spearman([1, 2], [1, 2]) is None


class FakeGateway:
    def __init__(self, fail=None):
        self.fail = fail
        self.kinds = []

    def transcribe(self, path, language_hint=None, route=None, data_kind=REAL, **_):
        self.kinds.append(data_kind)
        if self.fail and route.startswith(self.fail):
            raise PrivacyBlocked("confirm Zero Data Retention first")
        words = [Word("hello", 0.5, 0.9), Word("world", 1.0, 1.4)]
        return Transcription([Segment(0.5, 1.4, "hello world", words)], "en", 3.0, 1.0,
                             Usage("groq", "w", "transcribe", 900, 3.0, cost_usd=0.0001,
                                   free=False))

    def generate(self, system, user, schema=None, route=None, data_kind=REAL, **_):
        self.kinds.append(data_kind)
        if self.fail and route.startswith(self.fail):
            raise PrivacyBlocked("free tier: synthetic only")
        data = {"hook": 7, "payoff": 7, "emotion": 6, "standalone": 7, "shareability": 6,
                "verdict": "keep", "reason": "ok"}
        return Generation(json.dumps(data), data, Usage("x", "m", "generate"))


@pytest.fixture
def material(tmp_path):
    audio = tmp_path / "project" / "audio.wav"
    audio.parent.mkdir()
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "sine=frequency=300:duration=8", str(audio)], check=True)
    ref = tmp_path / "project" / "transcript.json"
    ref.write_text(json.dumps({"words": [{"word": "hello", "start": 2.5, "end": 2.9},
                                         {"word": "world", "start": 3.0, "end": 3.4}]}),
                   encoding="utf-8")
    return audio, ref


def test_transcription_suite_reports_metrics_and_unavailable_routes(tmp_path, material):
    audio, ref = material
    before = sorted(p.name for p in audio.parent.iterdir())
    rows = benchmark.transcription(FakeGateway(fail="groq"), audio, ref,
                                   ["local:medium", "groq:whisper-large-v3"], start=2.0,
                                   seconds=3.0, out=tmp_path / "bench")
    ok, missing = rows
    assert ok["status"] == "ok" and ok["wer"] == 0.0 and ok["timing_median_s"] == 0.0
    assert ok["cost_per_source_hour"] == pytest.approx(0.12)
    assert missing["status"] == "unavailable" and "Zero Data Retention" in missing["reason"]
    assert sorted(p.name for p in audio.parent.iterdir()) == before, "project untouched"
    report = benchmark.write_report(rows, tmp_path / "bench", "t")
    assert "unavailable" in report.read_text(encoding="utf-8")


def test_synthetic_suite_only_sends_synthetic_data():
    gw = FakeGateway(fail="gemini")
    rows = benchmark.synthetic(gw, ["groq:openai/gpt-oss-20b", "gemini:gemini-3.5-flash-lite"])
    assert rows[0]["status"] == "ok" and rows[0]["json_valid"] == 1.0
    assert rows[1]["status"] == "unavailable"
    assert set(gw.kinds) == {SYNTHETIC}
