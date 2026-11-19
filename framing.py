"""Phase 4: decide how to frame a clip for vertical (or square) output.

One low-resolution pass over the clip (5 frames per second) finds faces and
scene cuts. The clip is split into shots; each shot is a "face" shot (a person
is clearly visible) or a "wide" shot (gameplay, scenery, action). From that:

    track    - every shot has a face: a smooth crop that follows the person.
    fit      - no faces: the whole frame, scaled to fit, over a blurred fill,
               so gameplay HUDs and action at the edges stay visible.
    mixed    - both kinds of shot (typical for games with cutscenes): the
               tracked crop during face shots, the full frame otherwise.
    split    - two people side by side for most of a static clip (podcast,
               interview): each one gets half of the vertical frame.
    facecam  - a small, fixed face in a corner (streamer webcam over gameplay):
               the webcam on top, the gameplay below.

The tracker follows one person at a time: it sticks with the face it is on
unless another face is clearly bigger, ignores one-frame detection blips, only
moves when the person drifts out of the middle of the frame, eases into each
move, and snaps (no pan) on scene cuts.

Coordinates in this module are normalized (0..1 of the source width/height),
so the planning logic is pure and testable without video.
"""
import math
import os
import shutil
import subprocess
import tempfile
import urllib.request
from pathlib import Path

VERSION = 1
MODES = ("auto", "track", "center", "fit")
DET_FPS = 5
DET_W = 640
MIN_FACE = 0.025          # faces narrower than this (of the width) are ignored
FACE_SHOT = 0.5           # a shot is a "face" shot when >= half its frames have one
CUT_THRESHOLD = 0.45      # histogram change that counts as a scene cut
MIN_SHOT = 1.2            # seconds; shorter shots merge into a neighbour
STICKY_SWITCH = 1.6       # another face must be this much bigger to steal focus
DEADZONE = 0.12           # of the crop width: no camera move inside this
EASE = 0.30               # fraction of the remaining distance per sample
SETTLE = 0.03             # of the crop width: a move ends this close to centre
EDGE_MARGIN = 0.15        # keep the face at least this far (of crop) from the edge
SPLIT_RATIO = 0.6         # >= this share of frames show two separate people
FACECAM_RATIO = 0.7       # >= this share of frames show the same small face
FACECAM_MAX_W = 0.09
FACECAM_SPREAD = 0.03
YUNET_URL = ("https://github.com/opencv/opencv_zoo/raw/main/models/"
             "face_detection_yunet/face_detection_yunet_2023mar.onnx")
YUNET_MIN_BYTES = 100_000


# ------------------------------------------------------------- face model

def model_dir():
    """Where downloaded models live: $CHOPIFY_MODELS or ./models next to the code."""
    return Path(os.environ.get("CHOPIFY_MODELS")
                or Path(__file__).resolve().parent / "models")


def ensure_yunet(directory=None, legacy=None, url=YUNET_URL, timeout=30):
    """Path to a complete YuNet model, downloading it once; None on failure.

    The download goes to a temporary file that is renamed only when complete,
    so an interrupted download never leaves a broken model behind (Bug #3:
    the old code wrote straight to yunet.onnx and fell back to Haar forever).
    """
    directory = Path(directory or model_dir())
    target = directory / "yunet.onnx"
    for candidate in (target, Path(legacy) if legacy else None):
        if candidate and candidate.is_file():
            if candidate.stat().st_size >= YUNET_MIN_BYTES:
                return candidate
            try:
                candidate.unlink()          # a truncated file from the old code
            except OSError:
                pass
    try:
        directory.mkdir(parents=True, exist_ok=True)
        fd, temp = tempfile.mkstemp(prefix="yunet-", suffix=".part", dir=directory)
        os.close(fd)
        try:
            with urllib.request.urlopen(url, timeout=timeout) as response, \
                    open(temp, "wb") as handle:
                shutil.copyfileobj(response, handle)
            if Path(temp).stat().st_size < YUNET_MIN_BYTES:
                raise OSError("downloaded face model is incomplete")
            os.replace(temp, target)
        finally:
            if os.path.exists(temp):
                os.unlink(temp)
        return target
    except Exception as exc:  # network, disk, permissions
        print(f"  Face model download failed ({exc}); using the basic detector.",
              flush=True)
        return None


def make_detector(legacy=None):
    """("yunet"|"haar", detector)."""
    import cv2
    model = ensure_yunet(legacy=legacy)
    if model is not None:
        try:
            return "yunet", cv2.FaceDetectorYN.create(
                str(model), "", (DET_W, 360), 0.6, 0.3, 5000)
        except Exception as exc:
            print(f"  Face model failed to load ({exc}); using the basic detector.",
                  flush=True)
    return "haar", cv2.CascadeClassifier(
        cv2.data.haarcascades + "haarcascade_frontalface_default.xml")


# ------------------------------------------------------------- analysis

def analyze(source, start, duration, workdir, legacy_model=None):
    """Detect faces and cuts: {"times", "faces", "cuts"} (normalized)."""
    import cv2
    workdir = Path(workdir)
    fd, det_path = tempfile.mkstemp(prefix="_det-", suffix=".mp4", dir=workdir)
    os.close(fd)
    try:
        subprocess.run(
            ["ffmpeg", "-y", "-v", "error", "-ss", f"{start:.3f}", "-i", str(source),
             "-t", f"{duration:.3f}", "-vf", f"fps={DET_FPS},scale={DET_W}:-2",
             "-an", det_path], check=True,
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        kind, detector = make_detector(legacy_model)
        capture = cv2.VideoCapture(det_path)
        times, faces, cuts, bars = [], [], [], []
        previous = None
        index = 0
        while True:
            ok, frame = capture.read()
            if not ok:
                break
            height, width = frame.shape[:2]
            found = []
            if kind == "yunet":
                detector.setInputSize((width, height))
                _, raw = detector.detect(frame)
                for row in (raw if raw is not None else []):
                    found.append(((row[0] + row[2] / 2) / width,
                                  (row[1] + row[3] / 2) / height,
                                  row[2] / width, row[3] / height, float(row[14])))
            else:
                gray = cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)
                for x, y, w, h in detector.detectMultiScale(gray, 1.1, 5,
                                                            minSize=(24, 24)):
                    found.append(((x + w / 2) / width, (y + h / 2) / height,
                                  w / width, h / height, 1.0))
            hist = cv2.calcHist([cv2.cvtColor(frame, cv2.COLOR_BGR2HSV)],
                                [0, 1], None, [16, 8], [0, 180, 0, 256])
            hist = cv2.normalize(hist, hist).flatten()
            change = (0.0 if previous is None else
                      float(cv2.compareHist(previous, hist,
                                            cv2.HISTCMP_BHATTACHARYYA)))
            previous = hist
            times.append(index / float(DET_FPS))
            faces.append([f for f in found if f[2] >= MIN_FACE])
            cuts.append(change)
            bars.append(black_bars(cv2.cvtColor(frame, cv2.COLOR_BGR2GRAY)))
            index += 1
        capture.release()
        return {"times": times, "faces": faces, "cuts": cuts, "bars": bars,
                "detector": kind}
    finally:
        try:
            os.unlink(det_path)
        except OSError:
            pass


def black_bars(gray, luma=14, spread=8):
    """(top, bottom) letterbox height as a fraction of one grayscale frame."""
    height = gray.shape[0]
    means = gray.mean(axis=1)
    spreads = gray.std(axis=1)
    black = [(m < luma and s < spread) for m, s in zip(means, spreads)]
    top = next((i for i, b in enumerate(black) if not b), height)
    bottom = next((i for i, b in enumerate(reversed(black)) if not b), height)
    if top >= height:                       # an all-black frame says nothing
        return (0.0, 0.0)
    return (top / height, bottom / height)


# ------------------------------------------------------------- planning

LETTERBOX_MIN = 0.03      # bars thinner than this are left alone
LETTERBOX_SHARE = 0.75    # bars must be present in this share of frames
# The vertical crop already cuts most of the frame away, so trimming bars that
# are missing from a few shots costs little; visible bars cost a lot.
LETTERBOX_CROP_SHARE = 0.6


def letterbox(bars, frames=None, share=LETTERBOX_SHARE):
    """(top, bottom) bars present in at least ``share`` of the frames, else (0, 0)."""
    chosen = [bars[i] for i in (frames if frames is not None else range(len(bars)))]
    if not chosen:
        return (0.0, 0.0)
    rank = min(len(chosen) - 1, int(len(chosen) * (1 - share)))
    top = sorted(b[0] for b in chosen)[rank]
    bottom = sorted(b[1] for b in chosen)[rank]
    if top < LETTERBOX_MIN or bottom < LETTERBOX_MIN or top + bottom > 0.4:
        return (0.0, 0.0)
    return (round(top, 4), round(bottom, 4))


def shots_from(times, cuts, duration, threshold=CUT_THRESHOLD, min_shot=MIN_SHOT):
    """[(start, end)] shots; very short shots join the previous one."""
    if not times:
        return [(0.0, float(duration))]
    bounds = [0.0] + [t for t, c in zip(times, cuts) if c >= threshold and t > 0]
    bounds.append(float(duration))
    shots = []
    for a, b in zip(bounds, bounds[1:]):
        if b - a <= 0:
            continue
        if shots and (b - a < min_shot or shots[-1][1] - shots[-1][0] < min_shot):
            shots[-1] = (shots[-1][0], b)
        else:
            shots.append((a, b))
    return shots or [(0.0, float(duration))]


def _frames_in(times, a, b):
    return [i for i, t in enumerate(times) if a <= t < b]


def classify_shots(times, faces, shots):
    """[(start, end, "face"|"wide")], adjacent same-kind shots merged."""
    result = []
    for a, b in shots:
        frames = _frames_in(times, a, b)
        hits = sum(1 for i in frames if faces[i])
        kind = "face" if frames and hits / len(frames) >= FACE_SHOT else "wide"
        if result and result[-1][2] == kind:
            result[-1] = (result[-1][0], b, kind)
        else:
            result.append((a, b, kind))
    return result


def _largest(faces):
    return max(faces, key=lambda f: f[2] * f[3])


def pick_targets(times, faces, shots):
    """One x per frame: stick with the current person, switch only for a
    clearly bigger face, restart at every shot. None where nobody is seen."""
    starts = {s[0] for s in shots}
    targets = []
    current = None
    for t, found in zip(times, faces):
        if any(abs(t - s) < 1e-6 for s in starts):
            current = None
        if not found:
            targets.append(None)
            continue
        biggest = _largest(found)
        if current is None:
            chosen = biggest
        else:
            nearest = min(found, key=lambda f: abs(f[0] - current))
            chosen = (biggest if biggest[2] * biggest[3] >=
                      STICKY_SWITCH * nearest[2] * nearest[3] else nearest)
        current = chosen[0]
        targets.append(chosen[0])
    return targets


def _median(values):
    ordered = sorted(values)
    return ordered[len(ordered) // 2]


def smooth_path(times, targets, shots, crop_w):
    """Camera centre per frame (normalized), clamped so the crop stays inside."""
    half = crop_w / 2.0
    low, high = half, 1.0 - half
    # fill gaps: hold the last seen position, or the next one at the start
    filled = list(targets)
    last = None
    for i, value in enumerate(filled):
        if value is None:
            filled[i] = last
        else:
            last = value
    nxt = None
    for i in range(len(filled) - 1, -1, -1):
        if filled[i] is None:
            filled[i] = nxt
        else:
            nxt = filled[i]
    if all(v is None for v in filled):
        return [(t, 0.5 if low <= 0.5 <= high else low) for t in times]
    # median of 5 removes single-frame detection blips
    cleaned = []
    for i in range(len(filled)):
        window = [v for v in filled[max(0, i - 2):i + 3] if v is not None]
        cleaned.append(_median(window))
    starts = [s[0] for s in shots]
    path, camera, moving = [], None, False
    deadzone = DEADZONE * crop_w
    settle = SETTLE * crop_w
    limit = half - EDGE_MARGIN * crop_w
    for t, target in zip(times, cleaned):
        if camera is None or any(abs(t - s) < 1e-6 for s in starts):
            camera, moving = target, False      # snap on a new shot
        else:
            offset = target - camera
            # Hold still for small drifts; once the person leaves the dead
            # zone, ease all the way back to centre, then hold again.
            if moving or abs(offset) > deadzone:
                camera += EASE * offset
                moving = abs(target - camera) > settle
            if target - camera > limit:         # never let the face leave
                camera = target - limit
            elif camera - target > limit:
                camera = target + limit
        camera = min(max(camera, low), high)
        path.append((t, camera))
    return path


def detect_split(times, faces, shots, crop_w):
    """(left_x, right_x) when two separate people share a static clip."""
    if len(shots) > 2 or not times:
        return None
    pairs = []
    for found in faces:
        if len(found) >= 2:
            two = sorted(sorted(found, key=lambda f: -f[2] * f[3])[:2],
                         key=lambda f: f[0])
            if two[1][0] - two[0][0] >= crop_w * 0.8:
                pairs.append((two[0][0], two[1][0]))
    if len(pairs) / len(times) < SPLIT_RATIO:
        return None
    return (_median([p[0] for p in pairs]), _median([p[1] for p in pairs]))


def detect_facecam(times, faces):
    """(x, y, w, h) of a fixed webcam face in a corner, or None."""
    if not times:
        return None
    small = [min(found, key=lambda f: f[2]) for found in faces if found]
    small = [f for f in small if f[2] <= FACECAM_MAX_W]
    if len(small) / len(times) < FACECAM_RATIO:
        return None
    x = _median([f[0] for f in small])
    y = _median([f[1] for f in small])
    steady = [f for f in small if abs(f[0] - x) <= FACECAM_SPREAD
              and abs(f[1] - y) <= FACECAM_SPREAD]
    if len(steady) / len(times) < FACECAM_RATIO:
        return None
    corner = (x < 0.3 or x > 0.7) and (y < 0.4 or y > 0.6)
    if not corner:
        return None
    return (x, y, _median([f[2] for f in steady]), _median([f[3] for f in steady]))


def plan(analysis, duration, crop_w, requested="auto"):
    """Framing decision for one clip (see the module docstring)."""
    times = analysis.get("times") or []
    faces = analysis.get("faces") or []
    cuts = analysis.get("cuts") or [0.0] * len(times)
    shots = shots_from(times, cuts, duration)
    kinds = classify_shots(times, faces, shots)
    face_frames = sum(1 for f in faces if f)
    stats = {"version": VERSION, "frames": len(times), "shots": len(shots),
             "face_ratio": round(face_frames / len(times), 3) if times else 0.0,
             "detector": analysis.get("detector")}
    result = {"mode": None, "segments": [], "path": [], "centers": None,
              "facecam": None, "stats": stats,
              "bars": {"crop": (0.0, 0.0), "fit": (0.0, 0.0)}}
    bars = analysis.get("bars") or []
    if requested == "center":
        result["mode"] = "center"
        return result
    if requested == "fit":
        result["mode"] = "fit"
        return result
    if requested == "auto":
        facecam = detect_facecam(times, faces)
        if facecam is not None:
            result.update(mode="facecam", facecam=facecam)
            return result
        centers = detect_split(times, faces, shots, crop_w)
        if centers is not None:
            result.update(mode="split", centers=centers)
            return result
    has_face = any(k == "face" for _, _, k in kinds)
    has_wide = any(k == "wide" for _, _, k in kinds)
    if requested == "auto" and not has_face:
        result["mode"] = "fit"
        result["bars"]["fit"] = letterbox(bars)
        stats["letterbox"] = result["bars"]["fit"]
        return result
    mixed = requested == "auto" and has_wide
    if bars:
        face_idx = [i for i, t in enumerate(times)
                    if any(a <= t < b for a, b, k in kinds if k == "face")]
        wide_idx = [i for i in range(len(times)) if i not in set(face_idx)]
        result["bars"]["crop"] = letterbox(bars, face_idx if mixed else None,
                                           LETTERBOX_CROP_SHARE)
        if mixed:
            result["bars"]["fit"] = letterbox(bars, wide_idx)
        stats["letterbox"] = result["bars"]["crop"]
    # Cropping off letterbox bars makes the usable picture shorter, so the
    # vertical crop that fits inside it is narrower too.
    effective = crop_w * (1.0 - sum(result["bars"]["crop"]))
    targets = pick_targets(times, faces, shots)
    result["path"] = smooth_path(times, targets, shots, effective)
    if mixed:
        result["mode"] = "mixed"
        result["segments"] = [(round(a, 2), round(b, 2)) for a, b, k in kinds
                              if k == "face"]
    else:
        result["mode"] = "track"
    return result


def sendcmd_lines(path, crop_w_px, source_w, target="crop@track", rate=30.0):
    """ffmpeg sendcmd script moving the crop smoothly (interpolated at 30 Hz)."""
    if not path:
        return [f"0.00 {target} x 0;"]
    lines, last = [], None
    end = path[-1][0]
    step = 1.0 / rate
    t, k = 0.0, 0
    while t <= end + 1e-9:
        while k + 1 < len(path) and path[k + 1][0] <= t:
            k += 1
        if k + 1 < len(path):
            (t0, c0), (t1, c1) = path[k], path[k + 1]
            share = 0.0 if t1 <= t0 else (t - t0) / (t1 - t0)
            centre = c0 + (c1 - c0) * min(max(share, 0.0), 1.0)
        else:
            centre = path[k][1]
        x = int(round(centre * source_w - crop_w_px / 2.0))
        x = max(0, min(source_w - crop_w_px, x))
        if last is None or abs(x - last) >= 2:
            lines.append(f"{t:.3f} {target} x {x};")
            last = x
        t += step
    return lines
