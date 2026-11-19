"""Phase 4: framing decisions for vertical output (pure logic, no video)."""
import io
import urllib.request

import numpy as np
import pytest

import framing

CROP = 608 / 1920          # a 9:16 crop of a 16:9 source, normalized


def face(x, w=0.06, y=0.4):
    return (x, y, w, w * 1.7, 0.9)


def analysis(per_frame, cuts=None, bars=None, fps=framing.DET_FPS):
    n = len(per_frame)
    return {"times": [i / fps for i in range(n)], "faces": per_frame,
            "cuts": cuts or [0.0] * n, "bars": bars or [(0.0, 0.0)] * n,
            "detector": "yunet"}


# ------------------------------------------------------------ shots

def test_scene_cuts_split_shots_and_tiny_shots_merge():
    times = [i / 5 for i in range(50)]           # 10 s
    cuts = [0.0] * 50
    cuts[20] = 0.9                               # cut at 4.0 s
    cuts[22] = 0.9                               # 0.4 s flash -> merged
    shots = framing.shots_from(times, cuts, 10.0)
    assert len(shots) == 2 and 4.0 <= shots[0][1] <= 4.4
    assert shots[0][0] == 0.0 and shots[-1][1] == 10.0


def test_face_and_wide_shots_are_classified():
    frames = [[face(0.5)]] * 25 + [[]] * 25
    a = analysis(frames)
    shots = [(0.0, 5.0), (5.0, 10.0)]
    assert framing.classify_shots(a["times"], a["faces"], shots) == [
        (0.0, 5.0, "face"), (5.0, 10.0, "wide")]


# ------------------------------------------------------------ decisions

def test_no_faces_keeps_the_whole_frame():
    plan = framing.plan(analysis([[]] * 40), 8.0, CROP)
    assert plan["mode"] == "fit"


def test_faces_throughout_are_tracked():
    plan = framing.plan(analysis([[face(0.3)]] * 40), 8.0, CROP)
    assert plan["mode"] == "track" and plan["path"]


def test_cutscene_then_gameplay_is_mixed():
    frames = [[face(0.5)]] * 30 + [[]] * 30
    cuts = [0.0] * 60
    cuts[30] = 0.9
    plan = framing.plan(analysis(frames, cuts), 12.0, CROP)
    assert plan["mode"] == "mixed"
    assert plan["segments"] == [(0.0, 6.0)]


@pytest.mark.parametrize("requested", ["track", "center", "fit"])
def test_requested_mode_wins(requested):
    frames = [[face(0.5)]] * 30 + [[]] * 30
    plan = framing.plan(analysis(frames), 12.0, CROP, requested)
    assert plan["mode"] == requested


def test_two_people_side_by_side_get_a_split_screen():
    frames = [[face(0.25), face(0.75)]] * 50
    plan = framing.plan(analysis(frames), 10.0, CROP)
    assert plan["mode"] == "split"
    left, right = plan["centers"]
    assert left == pytest.approx(0.25) and right == pytest.approx(0.75)


def test_two_people_close_together_are_tracked_not_split():
    frames = [[face(0.45), face(0.55)]] * 50
    assert framing.plan(analysis(frames), 10.0, CROP)["mode"] == "track"


def test_fixed_small_corner_face_is_a_facecam():
    frames = [[face(0.88, w=0.05, y=0.82)]] * 50
    plan = framing.plan(analysis(frames), 10.0, CROP)
    assert plan["mode"] == "facecam"
    assert plan["facecam"][0] == pytest.approx(0.88)


def test_moving_small_face_is_not_a_facecam():
    frames = [[face(0.1 + i * 0.015, w=0.05, y=0.82)] for i in range(50)]
    assert framing.plan(analysis(frames), 10.0, CROP)["mode"] != "facecam"


# ------------------------------------------------------------ tracking

def test_tracker_sticks_with_the_current_person():
    frames = [[face(0.3, w=0.08)]] * 5 + [[face(0.3, w=0.08), face(0.7, w=0.09)]] * 10
    a = analysis(frames)
    targets = framing.pick_targets(a["times"], a["faces"], [(0.0, 3.0)])
    assert all(t == pytest.approx(0.3) for t in targets)


def test_tracker_switches_to_a_clearly_bigger_face():
    frames = [[face(0.3, w=0.05)]] * 5 + [[face(0.3, w=0.05), face(0.7, w=0.10)]] * 10
    a = analysis(frames)
    targets = framing.pick_targets(a["times"], a["faces"], [(0.0, 3.0)])
    assert targets[-1] == pytest.approx(0.7)


def test_small_jitter_does_not_move_the_camera():
    targets = [0.5 + (0.01 if i % 2 else -0.01) for i in range(40)]
    times = [i / 5 for i in range(40)]
    path = framing.smooth_path(times, targets, [(0.0, 8.0)], CROP)
    assert len({round(c, 6) for _, c in path}) == 1


def test_big_moves_are_eased_and_the_face_stays_in_frame():
    targets = [0.3] * 10 + [0.7] * 30
    times = [i / 5 for i in range(40)]
    path = framing.smooth_path(times, targets, [(0.0, 8.0)], CROP)
    centres = [c for _, c in path]
    steps = [abs(b - a) for a, b in zip(centres, centres[1:])]
    assert max(steps) < 0.4, "no instant jump within a shot"
    limit = CROP / 2 - framing.EDGE_MARGIN * CROP
    assert all(abs(t - c) <= limit + 1e-9 for t, c in zip(targets, centres))
    # after a move the person is centred again
    assert centres[-1] == pytest.approx(0.7, abs=framing.SETTLE * CROP + 1e-6)


def test_camera_snaps_on_a_scene_cut():
    targets = [0.3] * 20 + [0.7] * 20
    times = [i / 5 for i in range(40)]
    path = framing.smooth_path(times, targets, [(0.0, 4.0), (4.0, 8.0)], CROP)
    assert path[20][1] == pytest.approx(0.7)


def test_single_frame_blip_is_ignored():
    targets = [0.5] * 20
    targets[10] = 0.9
    times = [i / 5 for i in range(20)]
    path = framing.smooth_path(times, targets, [(0.0, 4.0)], CROP)
    assert all(c == pytest.approx(0.5) for _, c in path)


def test_crop_never_leaves_the_picture():
    times = [i / 5 for i in range(10)]
    for edge in (0.0, 1.0):
        path = framing.smooth_path(times, [edge] * 10, [(0.0, 2.0)], CROP)
        assert all(CROP / 2 - 1e-9 <= c <= 1 - CROP / 2 + 1e-9 for _, c in path)


def test_gaps_hold_the_last_position():
    targets = [0.3] * 5 + [None] * 10 + [0.3] * 5
    times = [i / 5 for i in range(20)]
    path = framing.smooth_path(times, targets, [(0.0, 4.0)], CROP)
    assert all(c == pytest.approx(0.3) for _, c in path)


def test_sendcmd_is_smooth_and_clamped():
    lines = framing.sendcmd_lines([(0.0, 0.3), (1.0, 0.7)], 608, 1920)
    xs = [int(line.split()[-1].rstrip(";")) for line in lines]
    times = [float(line.split()[0]) for line in lines]
    assert lines[0].startswith("0.000 crop@track x ")
    assert times == sorted(times) and len(lines) > 10
    assert all(0 <= x <= 1920 - 608 for x in xs)
    assert xs[-1] == round(0.7 * 1920 - 304)


# ------------------------------------------------------------ letterbox

def test_black_bars_are_measured():
    frame = np.full((100, 160), 120, dtype=np.uint8)
    frame[:6] = 0
    frame[-8:] = 0
    assert framing.black_bars(frame) == (0.06, 0.08)
    assert framing.black_bars(np.zeros((100, 160), dtype=np.uint8)) == (0.0, 0.0)


def test_letterbox_needs_bars_in_most_frames():
    bars = [(0.06, 0.06)] * 8 + [(0.0, 0.0)] * 2
    assert framing.letterbox(bars) == (0.06, 0.06)
    assert framing.letterbox([(0.06, 0.06)] * 5 + [(0.0, 0.0)] * 5) == (0.0, 0.0)
    assert framing.letterbox([(0.01, 0.01)] * 10) == (0.0, 0.0)


def test_letterboxed_cutscene_crop_is_narrower():
    frames = [[face(0.5)]] * 40
    bars = [(0.06, 0.06)] * 40
    plan = framing.plan(analysis(frames, bars=bars), 8.0, CROP)
    assert plan["bars"]["crop"] == (0.06, 0.06)
    assert plan["stats"]["letterbox"] == (0.06, 0.06)


# ------------------------------------------------------------ face model (Bug #3)

class _Response(io.BytesIO):
    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False


def test_model_download_is_atomic(tmp_path, monkeypatch):
    def broken(url, timeout=None):
        raise OSError("connection reset")
    monkeypatch.setattr(urllib.request, "urlopen", broken)
    assert framing.ensure_yunet(tmp_path) is None
    assert list(tmp_path.iterdir()) == [], "no partial model left behind"


def test_truncated_download_is_rejected(tmp_path, monkeypatch):
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda url, timeout=None: _Response(b"x" * 1000))
    assert framing.ensure_yunet(tmp_path) is None
    assert not (tmp_path / "yunet.onnx").exists()


def test_complete_download_is_used(tmp_path, monkeypatch):
    payload = b"y" * framing.YUNET_MIN_BYTES
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda url, timeout=None: _Response(payload))
    path = framing.ensure_yunet(tmp_path)
    assert path == tmp_path / "yunet.onnx" and path.read_bytes() == payload
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda *a, **k: pytest.fail("must not download again"))
    assert framing.ensure_yunet(tmp_path) == path


def test_broken_model_from_the_old_code_is_replaced(tmp_path, monkeypatch):
    legacy = tmp_path / "old" / "yunet.onnx"
    legacy.parent.mkdir()
    legacy.write_bytes(b"partial")
    payload = b"z" * framing.YUNET_MIN_BYTES
    monkeypatch.setattr(urllib.request, "urlopen",
                        lambda url, timeout=None: _Response(payload))
    path = framing.ensure_yunet(tmp_path / "models", legacy=legacy)
    assert not legacy.exists() and path.read_bytes() == payload


def test_models_live_in_the_configured_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("CHOPIFY_MODELS", str(tmp_path))
    assert framing.model_dir() == tmp_path
