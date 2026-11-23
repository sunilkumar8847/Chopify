"""Actual ffmpeg renders catch filtergraph, codec, path and subtitle regressions."""
import json
import shutil
import subprocess
from pathlib import Path

import pytest
import render_clips
import branding

pytestmark = pytest.mark.skipif(not shutil.which("ffmpeg"), reason="ffmpeg not installed")


@pytest.fixture
def source(tmp_path):
    path = tmp_path / "source with spaces.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=640x360:rate=12:duration=3", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=3", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(path)], check=True)
    return path


@pytest.mark.parametrize("layout,preview", [("single", True), ("split", True), ("split", False)])
def test_render_playable_with_subtitles_and_brand(source, tmp_path, layout, preview):
    logo = tmp_path / "logo.png"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "color=c=lime:s=80x40", "-frames:v", "1", str(logo)], check=True)
    preset = tmp_path / "brand.json"
    preset.write_text(json.dumps({"font": "Arial", "color": "#FF00FF", "logo": str(logo)}))
    out_dir = tmp_path / "render output"
    out_dir.mkdir()
    words = [{"word": "Hello", "start": 0.5, "end": 1.0},
             {"word": "world!", "start": 1.1, "end": 2.4}]
    out = render_clips.render({"id": "001", "start": 0.5, "end": 2.5, "hook": "same hook"},
        words, source, 640, 360, tmp_path, "9:16" if layout == "split" else "16:9",
        out_dir=out_dir, layout=layout, preview=preview, brand=branding.load_brand(preset))
    streams = json.loads(subprocess.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json",
                                        str(out)], capture_output=True, text=True, check=True).stdout)["streams"]
    assert {s["codec_name"] for s in streams} >= {"h264", "aac"}
    assert "00:00:00,000" in out.with_suffix(".srt").read_text()
    if layout == "split":
        video = next(s for s in streams if s["codec_type"] == "video")
        expected = render_clips.PREVIEW["9:16"] if preview else render_clips.ASPECTS["9:16"][:2]
        assert (video["width"], video["height"]) == expected
        assert video["pix_fmt"] == "yuv420p"
    if not preview:
        assert out.with_suffix(".png").is_file()
        assert out.with_suffix(".meta.json").is_file()


def test_brand_colors_appear_in_ass(tmp_path):
    output = tmp_path / "cap.ass"
    preset = tmp_path / "brand.json"
    preset.write_text(json.dumps({"font": "Georgia", "color": "#112233", "highlight": "#ABCDEF"}))
    render_clips.build_ass([{"word": "hello", "start": 0, "end": 1}], 0, 1, output,
                          1920, 1080, 72, 95, brand=branding.load_brand(preset))
    ass = output.read_text()
    assert "Georgia" in ass
    assert "&H00332211" in ass
    assert "&H00EFCDAB" in ass


def test_tightened_srt_tracks_actual_shortened_video(source, tmp_path):
    words = [{"word": "Hello", "start": 0.1, "end": 0.5},
             {"word": "um", "start": 0.6, "end": 1.0},
             {"word": "world.", "start": 2.0, "end": 2.5}]
    out = render_clips.render({"start": 0, "end": 3, "hook": "tightened"}, words,
        source, 640, 360, tmp_path, "16:9", out_dir=tmp_path / "tight", do_tighten=True, preview=True)
    import tighten
    keeps = tighten.plan_cuts(words, 0, 3)
    _, expected = tighten.remap_words(words, 0, keeps)
    duration = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", str(out)], capture_output=True, text=True, check=True).stdout)
    assert duration == pytest.approx(expected, abs=0.25)
    srt = out.with_suffix(".srt").read_text()
    assert "world." in srt
    assert " um " not in srt


def test_boxed_pop_caption_render_and_matching_srt_groups(source, tmp_path):
    words = [{"word": "Hello", "start": 0.5, "end": 0.8},
             {"word": "skip", "start": 0.9, "end": 1.0, "hidden": True},
             {"word": "world!", "start": 1.1, "end": 1.4},
             {"word": "again", "start": 1.5, "end": 2.4}]
    out = render_clips.render(
        {"id": "001", "start": 0.5, "end": 2.5, "hook": "boxed pop"},
        words, source, 640, 360, tmp_path, "16:9", out_dir=tmp_path / "render",
        preview=True, caption={"background": "box", "background_color": "#112233",
                               "background_opacity": 70, "animation": "pop",
                               "words_per_cue": 2, "uppercase": True})
    streams = json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(out)],
        capture_output=True, text=True, check=True).stdout)["streams"]
    assert {s["codec_name"] for s in streams} >= {"h264", "aac"}
    srt = out.with_suffix(".srt").read_text(encoding="utf-8")
    assert srt.count("-->") == 2
    assert "skip" not in srt
    assert "Hello world!" in srt


def test_cli_render_preserves_project_settings_and_copies_exports(source, tmp_path):
    import os
    import sys
    import projects
    path = tmp_path / "saved project"
    projects.init_project(path, str(source), {"aspect": "9:16", "layout": "split"})
    projects.atomic_json(path / "transcript.json", {"video": str(source), "duration": 3,
        "words": [{"word": "Hello.", "start": 0.5, "end": 1.5}]})
    projects.atomic_json(path / "segments.json", [{"start": 0.5, "end": 2, "hook": "hello", "overall": 6}])
    export = tmp_path / "export folder"
    result = subprocess.run([sys.executable, str(Path(__file__).resolve().parents[1] / "chopify.py"),
        "--project", str(path), "--action", "render", "--preview", "--no-preflight", "--out", str(export)],
        capture_output=True, text=True, env={**os.environ, "PYTHONIOENCODING": "utf-8"})
    assert result.returncode == 0, result.stderr + result.stdout
    assert projects.read_project(path)["options"]["layout"] == "split"
    assert len(list(export.glob("*.mp4"))) == len(list(export.glob("*.srt"))) == 1


def test_render_from_section_offset_reads_local_video_not_source_start(tmp_path):
    source = tmp_path / "sections.mp4"
    subprocess.run([
        "ffmpeg", "-v", "error", "-y",
        "-f", "lavfi", "-i", "color=c=green:s=320x180:d=3",
        "-f", "lavfi", "-i", "color=c=red:s=320x180:d=3",
        "-f", "lavfi", "-i", "sine=frequency=440:duration=6",
        "-filter_complex", "[0:v][1:v]concat=n=2:v=1:a=0[v]",
        "-map", "[v]", "-map", "2:a", "-c:v", "libx264",
        "-pix_fmt", "yuv420p", "-c:a", "aac", "-shortest", str(source)],
        check=True)
    out = render_clips.render(
        {"id": "001", "start": 3.5, "end": 5.0, "hook": "offset section"},
        [{"word": "hello", "start": 3.5, "end": 4.0}], source, 320, 180,
        tmp_path, "16:9", out_dir=tmp_path / "render", preview=True,
        source_offset=3.0)
    frame = subprocess.run(
        ["ffmpeg", "-v", "error", "-ss", "0.2", "-i", str(out),
         "-frames:v", "1", "-f", "rawvideo", "-pix_fmt", "rgb24", "-"],
        capture_output=True, check=True).stdout
    red, green = frame[0], frame[1]
    assert green > 100 and red < 80
    assert "00:00:00,000" in out.with_suffix(".srt").read_text()


def test_hindi_words_render_roman_captions_srt_and_metadata(source, tmp_path, monkeypatch):
    """Output policy: burned-in captions, SRT, file name and meta are Roman."""
    import hinglish
    seen = {}
    real_build_ass = render_clips.build_ass

    def spy(words, *args, **kwargs):
        seen["ass_words"] = [w["word"] for w in words]
        return real_build_ass(words, *args, **kwargs)
    monkeypatch.setattr(render_clips, "build_ass", spy)
    words = [{"word": "आज", "start": 0.5, "end": 0.8},
             {"word": "मैंने", "start": 0.8, "end": 1.1},
             {"word": "पहली", "start": 1.1, "end": 1.4},
             {"word": "बार", "start": 1.4, "end": 1.7},
             {"word": "GTA", "start": 1.7, "end": 2.0},
             {"word": "खेला।", "start": 2.0, "end": 2.4}]
    out = render_clips.render(
        {"id": "001", "start": 0.5, "end": 2.5, "hook": "पहली बार GTA"},
        words, source, 640, 360, tmp_path, "9:16", out_dir=tmp_path / "render")
    srt = out.with_suffix(".srt").read_text(encoding="utf-8")
    meta = json.loads(out.with_suffix(".meta.json").read_text(encoding="utf-8"))
    assert seen["ass_words"][:6] == ["aaj", "maine", "pehli", "baar", "GTA", "khela."]
    cues = [block.splitlines()[2] for block in srt.strip().split("\n\n")]
    assert cues == ["aaj maine", "pehli baar", "GTA khela."]
    assert not hinglish.has_devanagari(srt)
    assert not hinglish.has_devanagari(out.name) and "pehli-baar-gta" in out.name
    assert meta["title"].startswith("Pehli baar GTA") and not hinglish.has_devanagari(json.dumps(meta, ensure_ascii=False))
    assert words[0]["word"] == "आज", "the caller's original words are untouched"


@pytest.mark.parametrize("aspect,size", [("9:16", (1080, 1920)), ("16:9", (1920, 1080))])
@pytest.mark.parametrize("framing", ["center", "fit"])
def test_exports_have_true_dimensions_and_square_pixels(source, tmp_path, aspect, size, framing):
    """1080x1920 must display as 1080x1920 (no stray sample aspect ratio)."""
    words = [{"word": "Hello", "start": 0.5, "end": 1.0}]
    out = render_clips.render({"id": "001", "start": 0.0, "end": 2.5, "hook": "hook"},
                              words, source, 640, 360, tmp_path, aspect, out_dir=tmp_path,
                              framing_mode=framing)
    video = next(s for s in json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(out)],
        capture_output=True, text=True, check=True).stdout)["streams"]
        if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == size
    assert video.get("sample_aspect_ratio", "1:1") in ("1:1", "0:1")
    duration = float(subprocess.run(["ffprobe", "-v", "error", "-show_entries", "format=duration",
                                     "-of", "csv=p=0", str(out)], capture_output=True, text=True,
                                    check=True).stdout)
    assert duration <= 120


def test_shorts_captions_sit_inside_the_safe_area():
    safe = render_clips.SAFE_AREAS["9:16"]
    margin, side = render_clips.caption_geometry("9:16", 1080, 1920, "bottom", 95)
    assert margin == round(safe["bottom"] * 1920) and margin >= 0.2 * 1920
    assert side == round(safe["side"] * 1080) and side > 80
    assert render_clips.caption_geometry("16:9", 1920, 1080, "bottom", 95) == (95, 80)


def test_facecam_layout_exports_a_true_9_16_frame(tmp_path, monkeypatch):
    """Regression: facecam-on-top exports were 1080x1920 with SAR 1955:1962
    (shown as 1080x1927)."""
    src = tmp_path / "game.mp4"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-i",
                    "testsrc2=size=1920x1080:rate=12:duration=3", "-f", "lavfi", "-i",
                    "sine=frequency=440:duration=3", "-c:v", "libx264", "-pix_fmt", "yuv420p",
                    "-c:a", "aac", "-shortest", str(src)], check=True)
    import framing
    monkeypatch.setattr(framing, "analyze", lambda *a, **k: {})
    monkeypatch.setattr(framing, "plan", lambda *a, **k: {
        "mode": "facecam", "facecam": (0.87, 0.18, 0.11, 0.17)})
    out = render_clips.render({"id": "001", "start": 0.0, "end": 2.5, "hook": "cam"},
                              [{"word": "Hi", "start": 0.5, "end": 1.0}], src, 1920, 1080,
                              tmp_path, "9:16", out_dir=tmp_path, framing_mode="auto")
    video = next(s for s in json.loads(subprocess.run(
        ["ffprobe", "-v", "error", "-show_streams", "-of", "json", str(out)],
        capture_output=True, text=True, check=True).stdout)["streams"]
        if s["codec_type"] == "video")
    assert (video["width"], video["height"]) == (1080, 1920)
    assert video.get("sample_aspect_ratio") in ("1:1", None), video.get("sample_aspect_ratio")


def test_interrupted_render_never_leaves_a_final_looking_clip(source, tmp_path, monkeypatch):
    """The encode writes "_partial-<name>"; only a verified file gets the real name."""
    out_dir = tmp_path / "clips"
    out_dir.mkdir()
    words = [{"word": "Hello", "start": 0.5, "end": 1.0}]
    seg = {"id": "001", "start": 0.5, "end": 2.5, "hook": "kept hook"}
    real_run = subprocess.run
    encoded = []

    def crash_during_encode(cmd, *args, **kwargs):
        if cmd and cmd[0] == "ffmpeg" and "-filter_complex" in cmd:
            encoded.append(Path(cmd[-1]).name)
            Path(out_dir / Path(cmd[-1]).name).write_bytes(b"half an mp4")
            raise KeyboardInterrupt("Studio killed mid-encode")
        return real_run(cmd, *args, **kwargs)
    monkeypatch.setattr(render_clips.subprocess, "run", crash_during_encode)
    with pytest.raises(KeyboardInterrupt):
        render_clips.render(seg, words, source, 640, 360, tmp_path, "9:16", out_dir=out_dir)
    assert encoded and encoded[0].startswith("_partial-")
    assert [p.name for p in out_dir.glob("*.mp4") if not p.name.startswith("_")] == []
    monkeypatch.setattr(render_clips.subprocess, "run", real_run)
    out = render_clips.render(seg, words, source, 640, 360, tmp_path, "9:16", out_dir=out_dir)
    assert out.name == encoded[0].removeprefix("_partial-") and out.exists()
    assert not list(out_dir.glob("_partial-*")), "the retry replaced the partial file"
    assert out.with_suffix(".srt").exists() and out.with_suffix(".meta.json").exists()
