"""Phase 4: Shorts rendering with real ffmpeg (framing, captions, audio, checks)."""
import json
import re
import shutil
import subprocess

import pytest

import captions
import framing
import projects
import render_clips

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")

WORDS = [{"word": "Nobody", "start": 0.3, "end": 0.6},
         {"word": "believed", "start": 0.62, "end": 0.9},
         {"word": "me.", "start": 0.92, "end": 1.1},
         {"word": "Then", "start": 2.5, "end": 2.7},
         {"word": "I", "start": 2.72, "end": 2.8},
         {"word": "won!", "start": 2.82, "end": 3.2}]


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "wide source.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=640x360:rate=24:duration=4", "-f", "lavfi", "-i",
                    "sine=frequency=330:duration=4:sample_rate=44100",
                    "-c:v", "libx264", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-shortest", str(path)], check=True)
    return path


def probe(path):
    data = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries",
         "format=duration:stream=codec_type,codec_name,width,height,sample_rate",
         "-of", "json", str(path)], capture_output=True, text=True, check=True).stdout)
    video = next(s for s in data["streams"] if s["codec_type"] == "video")
    audio = next((s for s in data["streams"] if s["codec_type"] == "audio"), None)
    return video, audio, float(data["format"]["duration"])


def decode_errors(path):
    result = subprocess.run(["ffmpeg", "-v", "error", "-i", str(path), "-f", "null", "-"],
                            capture_output=True, text=True)
    return result.returncode, result.stderr.strip()


def fake_analysis(monkeypatch, faces_for, bars=(0.0, 0.0), cut_at=None):
    def analyze(source, start, duration, workdir, legacy_model=None):
        n = int(duration * framing.DET_FPS)
        times = [i / framing.DET_FPS for i in range(n)]
        cuts = [0.9 if cut_at is not None and abs(t - cut_at) < 1e-6 else 0.0
                for t in times]
        return {"times": times, "faces": [faces_for(t) for t in times],
                "cuts": cuts, "bars": [bars] * n, "detector": "test"}
    monkeypatch.setattr(framing, "analyze", analyze)


def render(source, tmp_path, aspect="9:16", preview=False, **kwargs):
    return render_clips.render({"id": "001", "start": 0.0, "end": 3.5, "hook": "test clip"},
                               WORDS, source, 640, 360, tmp_path, aspect,
                               out_dir=tmp_path / "out", preview=preview, **kwargs)


# ------------------------------------------------------------ framing modes

@pytest.mark.parametrize("faces_for,mode", [
    (lambda t: [], "fit"),
    (lambda t: [(0.3, 0.4, 0.08, 0.14, 0.9)], "track"),
    (lambda t: [(0.5, 0.4, 0.08, 0.14, 0.9)] if t < 2.0 else [], "mixed"),
    (lambda t: [(0.2, 0.4, 0.08, 0.14, 0.9), (0.8, 0.4, 0.08, 0.14, 0.9)], "split"),
    (lambda t: [(0.9, 0.85, 0.05, 0.09, 0.9)], "facecam"),
])
def test_every_framing_renders_a_playable_vertical_clip(source, tmp_path, monkeypatch,
                                                        faces_for, mode):
    fake_analysis(monkeypatch, faces_for, cut_at=2.0 if mode == "mixed" else None)
    out = render(source, tmp_path, preview=True)
    video, audio, duration = probe(out)
    assert (video["width"], video["height"]) == render_clips.PREVIEW["9:16"]
    assert audio is not None and duration == pytest.approx(3.5, abs=0.2)
    assert decode_errors(out) == (0, "")


def test_final_vertical_export_is_1080x1920_with_checked_metadata(source, tmp_path,
                                                                  monkeypatch):
    fake_analysis(monkeypatch, lambda t: [(0.4, 0.4, 0.08, 0.14, 0.9)])
    out = render(source, tmp_path, loudnorm=True)
    video, audio, duration = probe(out)
    assert (video["width"], video["height"]) == (1080, 1920)
    assert video["codec_name"] == "h264" and audio["codec_name"] == "aac"
    assert audio["sample_rate"] == "48000"
    meta = json.loads(out.with_suffix(".meta.json").read_text(encoding="utf-8"))
    info = meta["render"]
    assert info["verified"]["ok"] is True and info["verified"]["width"] == 1080
    assert info["framing"]["mode"] == "track"
    assert info["loudness"]["two_pass"] is True
    assert info["caption_style"] == "shorts" and info["encoder"] == "x264"
    assert out.with_suffix(".png").is_file()


def test_letterbox_bars_are_cropped_away(source, tmp_path, monkeypatch):
    fake_analysis(monkeypatch, lambda t: [(0.5, 0.4, 0.08, 0.14, 0.9)],
                  bars=(0.1, 0.1))
    commands = []
    real_run = subprocess.run

    def spy(cmd, *args, **kwargs):
        commands.append([str(c) for c in cmd])
        return real_run(cmd, *args, **kwargs)
    monkeypatch.setattr(render_clips.subprocess, "run", spy)
    render(source, tmp_path, preview=True)
    graph = next(c[c.index("-filter_complex") + 1] for c in commands
                 if "-filter_complex" in c)
    # active picture is 360 - 2*36 = 288 px tall, starting 36 px down
    assert re.search(r"crop@track=\d+:288:\d+:36", graph)


def test_sixteen_nine_output_is_not_reframed(source, tmp_path, monkeypatch):
    monkeypatch.setattr(framing, "analyze",
                        lambda *a, **k: pytest.fail("16:9 needs no analysis"))
    out = render(source, tmp_path, aspect="16:9", preview=True)
    video, _, _ = probe(out)
    assert (video["width"], video["height"]) == render_clips.PREVIEW["16:9"]


def test_forced_fit_skips_face_analysis(source, tmp_path, monkeypatch):
    monkeypatch.setattr(framing, "analyze",
                        lambda *a, **k: pytest.fail("fit needs no analysis"))
    out = render(source, tmp_path, preview=True, framing_mode="fit")
    assert decode_errors(out)[0] == 0


def test_unknown_framing_is_rejected(source, tmp_path):
    with pytest.raises(ValueError, match="Framing"):
        render(source, tmp_path, preview=True, framing_mode="zoom")


# ------------------------------------------------------------ captions

def test_vertical_captions_sit_in_the_shorts_safe_area(tmp_path):
    margin, side = render_clips.caption_geometry("9:16", 1080, 1920, "bottom", 480)
    assert margin == 480 and side == 140          # above the title, clear of buttons
    assert render_clips.caption_geometry("9:16", 1080, 1920, "top", 480)[0] == 250
    assert render_clips.caption_geometry("16:9", 1920, 1080, "bottom", 95) == (95, 80)
    path = tmp_path / "cap.ass"
    render_clips.build_ass(WORDS, 0, 3.5, path, 1080, 1920, 88, margin, "shorts",
                           margin_lr=side)
    style = next(line for line in path.read_text().splitlines()
                 if line.startswith("Style: Cap"))
    assert style.endswith(",140,140,480,1")


def test_caption_words_are_synchronised_to_the_transcript(tmp_path):
    path = tmp_path / "cap.ass"
    render_clips.build_ass(WORDS, 0.2, 3.5, path, 1080, 1920, 88, 480, "shorts")
    events = [line for line in path.read_text().splitlines()
              if line.startswith("Dialogue")]
    starts = [line.split(",")[1] for line in events]
    expected = [render_clips.ass_time(w["start"] - 0.2) for w in WORDS]
    assert starts == expected, "each word lights up exactly when it is spoken"
    # the active word pops in the Shorts style
    assert all("\\fscx135" in line for line in events)


def test_cues_break_at_sentences_pauses_and_line_width():
    groups = captions.group_words(WORDS, 3)
    assert [[w["word"] for w in g] for g in groups] == [
        ["Nobody", "believed", "me."], ["Then", "I", "won!"]]
    long = [{"word": "UNBELIEVABLE", "start": i, "end": i + 0.5} for i in range(3)]
    assert len(captions.group_words(long, 3, max_chars=14)) == 3
    paused = [{"word": "a", "start": 0, "end": 0.2}, {"word": "b", "start": 2.0, "end": 2.2}]
    assert len(captions.group_words(paused, 3)) == 2


def test_srt_and_burned_captions_use_the_same_cues(tmp_path):
    ass, srt = tmp_path / "c.ass", tmp_path / "c.srt"
    render_clips.build_ass(WORDS, 0, 3.5, ass, 1080, 1920, 88, 480, "shorts",
                           max_chars=14)
    captions.write_srt(WORDS, 0, 3.5, srt, group_size=3, max_chars=14)
    cues = srt.read_text().count("-->")
    groups = captions.group_words(WORDS, 3, 14)
    assert cues == len(groups)
    assert len([l for l in ass.read_text().splitlines() if l.startswith("Dialogue")]) == len(WORDS)


def test_shorts_style_is_the_vertical_default_only():
    assert "shorts" in render_clips.CAPTION_STYLES
    assert render_clips.line_chars("9:16", 1080, 140, {"uppercase": True}, 88, 1920) == 13
    assert render_clips.line_chars("16:9", 1920, 80, {"uppercase": True}, 72, 1080) is None


def test_explicit_style_is_respected_on_vertical(source, tmp_path, monkeypatch):
    fake_analysis(monkeypatch, lambda t: [])
    out = render(source, tmp_path, style="clean", loudnorm=False)
    meta = json.loads(out.with_suffix(".meta.json").read_text(encoding="utf-8"))
    assert meta["render"]["caption_style"] == "clean"


# ------------------------------------------------------------ audio

def test_two_pass_loudness_reaches_the_platform_target(source, tmp_path, monkeypatch):
    fake_analysis(monkeypatch, lambda t: [])
    measured = render_clips.measure_loudness(source, 0, 3.5)
    assert measured is not None and measured["input_i"] < 0
    flt = render_clips.loudnorm_filter(measured)
    assert "measured_I=" in flt and "linear=true" in flt
    out = render(source, tmp_path, loudnorm=True)
    report = subprocess.run(["ffmpeg", "-hide_banner", "-nostats", "-i", str(out),
                             "-vn", "-af", "ebur128", "-f", "null", "-"],
                            capture_output=True, text=True).stderr
    integrated = float(re.findall(r"I:\s+(-?[\d.]+) LUFS", report)[-1])
    assert integrated == pytest.approx(render_clips.LOUDNESS["I"], abs=1.5)


def test_silence_skips_measurement(tmp_path):
    silent = tmp_path / "silent.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=black:s=320x180:d=2", "-f", "lavfi", "-i",
                    "anullsrc=r=44100:cl=stereo", "-t", "2", "-c:v", "libx264",
                    "-c:a", "aac", "-shortest", str(silent)], check=True)
    assert render_clips.measure_loudness(silent, 0, 2) is None
    assert render_clips.loudnorm_filter(None).startswith("loudnorm=I=-14")


# ------------------------------------------------------------ checks & encoders

def test_output_check_catches_wrong_size_and_duration(source, tmp_path):
    report = render_clips.verify_output(source, 1080, 1920, 10.0)
    assert report["ok"] is False
    assert any("size" in p for p in report["problems"])
    assert any("duration" in p for p in report["problems"])
    assert render_clips.verify_output(source, 640, 360, 4.0)["ok"] is True


def test_failed_check_fails_the_render(source, tmp_path, monkeypatch):
    fake_analysis(monkeypatch, lambda t: [])
    monkeypatch.setattr(render_clips, "verify_output",
                        lambda *a, **k: {"ok": False, "problems": ["no audio stream"]})
    with pytest.raises(ValueError, match="failed its check"):
        render(source, tmp_path, preview=True)


def test_hardware_encoder_failure_falls_back_to_x264(source, tmp_path, monkeypatch):
    fake_analysis(monkeypatch, lambda t: [])
    monkeypatch.setattr(render_clips, "pick_encoder", lambda: "nvenc")
    real_run = subprocess.run
    used = []

    def run(cmd, *args, **kwargs):
        cmd = [str(c) for c in cmd]
        if "-c:v" in cmd:
            used.append(cmd[cmd.index("-c:v") + 1])
            if cmd[cmd.index("-c:v") + 1] == "h264_nvenc":
                raise subprocess.CalledProcessError(1, cmd)
        return real_run(cmd, *args, **kwargs)
    monkeypatch.setattr(render_clips.subprocess, "run", run)
    out = render(source, tmp_path, preview=True)
    assert used[:2] == ["h264_nvenc", "libx264"] and out.is_file()


def test_encoder_choice_respects_the_environment(monkeypatch):
    monkeypatch.setenv("CHOPIFY_ENCODER", "x264")
    assert render_clips.pick_encoder() == "x264"
    args = render_clips.video_codec_args("nvenc", render_clips.FINAL_ENCODE)
    assert args[:2] == ["-c:v", "h264_nvenc"] and "-cq" in args


def test_parallel_renders_use_their_own_temporary_files(source, tmp_path, monkeypatch):
    fake_analysis(monkeypatch, lambda t: [(0.3, 0.4, 0.08, 0.14, 0.9)])
    seen = []
    real_run = subprocess.run

    def spy(cmd, *args, **kwargs):
        cmd = [str(c) for c in cmd]
        if "-filter_complex" in cmd:
            seen.append(cmd[cmd.index("-filter_complex") + 1])
        return real_run(cmd, *args, **kwargs)
    monkeypatch.setattr(render_clips.subprocess, "run", spy)
    for clip_id in ("001", "002"):
        render_clips.render({"id": clip_id, "start": 0, "end": 2, "hook": "same"},
                            WORDS, source, 640, 360, tmp_path, "9:16",
                            out_dir=tmp_path / "out", preview=True)
    assert "_caption-001.ass" in seen[0] and "_caption-002.ass" in seen[1]
    assert "_crop-001.cmd" in seen[0]
    assert not list((tmp_path / "out").glob("_caption*")), "temporary files removed"


# ------------------------------------------------------------ options

def test_project_options_for_framing_and_loudness():
    options = projects.validate_options({})
    assert options["framing"] == "auto" and options["loudnorm"] is True
    assert projects.validate_options({"framing": None})["framing"] == "auto"
    with pytest.raises(ValueError):
        projects.validate_options({"framing": "zoom"})


def test_saved_projects_keep_their_loudness_setting(tmp_path):
    path = tmp_path / "proj"
    projects.init_project(path, "recording.mp4", {"loudnorm": False})
    state = projects.read_project(path)
    assert projects.validate_options(state["options"])["loudnorm"] is False
