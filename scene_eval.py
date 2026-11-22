"""Scene-classification evaluation: label samples, score predictions, make fixtures.

    python scene_eval.py label  <video> [--every 10] [--out labels.html]
        Writes a self-contained HTML page with one thumbnail per interval and
        a label menu for each; "Download labels" saves labels.json.
    python scene_eval.py score  <video> <labels.json> [--no-ocr]
        Runs scenes.analyze and prints time-weighted results, including the
        acceptance targets (>= 95 % gameplay kept, >= 90 % intro/BRB/outro
        identified).
    python scene_eval.py sample <out.mp4> [plan]
        Builds a synthetic stream (starting soon, gameplay, menu, loading,
        BRB, outro) for tests and simulations.

labels.json: ``[{"start": 0, "end": 60, "label": "starting_soon"}, ...]``
"""
import argparse
import base64
import html
import json
import subprocess
import sys
import tempfile
from pathlib import Path

import scenes

TARGET_GAMEPLAY = 0.95
TARGET_BREAKS = 0.90
BREAKS = ("intro", "brb", "outro", "starting_soon")
FONT = "C\\:/Windows/Fonts/arial.ttf" if sys.platform == "win32" else \
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf"


# ----------------------------------------------------------------- scoring

def _overlap(a0, a1, b0, b1):
    return max(0.0, min(a1, b1) - max(a0, b0))


def score(predicted, labels, step=1.0):
    """Time-weighted comparison of predicted scenes against labelled spans."""
    seconds = {}
    total = {}
    for span in labels:
        t = float(span["start"])
        while t < float(span["end"]) - 1e-6:
            truth = span["label"]
            guess = scenes.label_at(predicted, t + step / 2)
            total[truth] = total.get(truth, 0.0) + step
            seconds.setdefault(truth, {})
            seconds[truth][guess] = seconds[truth].get(guess, 0.0) + step
            t += step
    gameplay = total.get("gameplay", 0.0)
    kept = sum(v for g, v in seconds.get("gameplay", {}).items() if g not in scenes.NON_GAMEPLAY)
    breaks_total = sum(total.get(b, 0.0) for b in BREAKS)
    # A break counts as identified when it is predicted as any non-gameplay
    # break label of the same family (starting_soon/intro are both "before").
    family = {"intro": {"intro", "starting_soon"}, "starting_soon": {"intro", "starting_soon"},
              "brb": {"brb"}, "outro": {"outro"}}
    found = sum(v for b in BREAKS for g, v in seconds.get(b, {}).items() if g in family[b])
    false_cut = gameplay - kept
    report = {
        "gameplay_seconds": gameplay,
        "gameplay_kept": round(kept / gameplay, 4) if gameplay else None,
        "breaks_seconds": breaks_total,
        "breaks_identified": round(found / breaks_total, 4) if breaks_total else None,
        "false_negative_gameplay_seconds": round(false_cut, 1),
        "false_positive_gameplay_seconds": round(sum(
            v for t, row in seconds.items() if t != "gameplay"
            for g, v in row.items() if g == "gameplay"), 1),
        "confusion": {t: {g: round(v, 1) for g, v in row.items()} for t, row in seconds.items()},
    }
    report["meets_targets"] = bool(
        (report["gameplay_kept"] is None or report["gameplay_kept"] >= TARGET_GAMEPLAY)
        and (report["breaks_identified"] is None or report["breaks_identified"] >= TARGET_BREAKS))
    return report


# ---------------------------------------------------------------- labelling

def label_page(video, every=10.0, out=None):
    """A standalone HTML labelling page (thumbnails inlined)."""
    video = Path(video)
    duration = float(subprocess.run(
        ["ffprobe", "-v", "error", "-show_entries", "format=duration", "-of",
         "default=nw=1:nk=1", str(video)], capture_output=True, text=True).stdout or 0)
    cells = []
    t = 0.0
    while t < duration:
        jpg = subprocess.run(["ffmpeg", "-v", "error", "-ss", f"{t:.2f}", "-i", str(video),
                              "-frames:v", "1", "-vf", "scale=240:-2", "-f", "image2pipe",
                              "-vcodec", "mjpeg", "-"], capture_output=True).stdout
        options = "".join(f"<option>{label}</option>" for label in scenes.LABELS)
        cells.append(
            f'<figure data-start="{t:.1f}" data-end="{min(duration, t + every):.1f}">'
            f'<img alt="frame at {t:.0f}s" src="data:image/jpeg;base64,'
            f'{base64.b64encode(jpg).decode()}"><figcaption>{int(t // 60)}:{int(t % 60):02d}'
            f'<select>{options}</select></figcaption></figure>')
        t += every
    page = f"""<!doctype html><meta charset="utf-8"><title>Label scenes</title>
<style>body{{font:14px Segoe UI,sans-serif;background:#15191b;color:#eee}}
main{{display:grid;grid-template-columns:repeat(auto-fill,minmax(250px,1fr));gap:8px}}
figure{{margin:0}}img{{width:100%}}select{{margin-left:8px}}</style>
<h1>{html.escape(video.name)}</h1><p>Pick a label per thumbnail (first option is gameplay;
the label applies until the next thumbnail). <button id="save">Download labels</button></p>
<main>{''.join(cells)}</main>
<script>document.getElementById('save').onclick=()=>{{const spans=[];
for(const f of document.querySelectorAll('figure')){{const l=f.querySelector('select').value,
s=+f.dataset.start,e=+f.dataset.end,last=spans[spans.length-1];
if(last&&last.label===l)last.end=e;else spans.push({{start:s,end:e,label:l}});}}
const a=document.createElement('a');a.href=URL.createObjectURL(new Blob([JSON.stringify(spans,null,1)]));
a.download='labels.json';a.click();}};</script>"""
    out = Path(out or video.with_suffix(".labels.html"))
    out.write_text(page, encoding="utf-8")
    return out


# ------------------------------------------------------------------ fixtures

DEFAULT_PLAN = [("starting_soon", 40), ("gameplay", 90), ("menu", 20), ("gameplay", 60),
                ("brb", 40), ("loading", 15), ("gameplay", 90), ("outro", 30)]


def _segment(kind, seconds, out, seed):
    size = "640x360"
    text = {"starting_soon": "Stream Starting Soon", "brb": "Be Right Back",
            "outro": "Thanks for watching!", "loading": "Loading...", "intro": "Intro",
            "menu": "PLAY\nSETTINGS\nQUIT"}.get(kind)
    if kind == "gameplay":
        video = (f"testsrc2=size={size}:rate=15,hue=h={seed * 40},"
                 f"drawbox=x=0:y=0:w=640:h=34:color=black@0.8:t=fill,"
                 f"drawtext=fontfile='{FONT}':text='HP 100   AMMO 30   K 4':x=10:y=8:"
                 f"fontsize=18:fontcolor=white,"
                 f"drawbox=x=540:y=250:w=90:h=100:color=white@0.9:t=3")
        audio = f"anoisesrc=d={seconds}:c=pink:a=0.25:seed={seed}"
    else:
        lines = text.split("\n")
        draws = ",".join(
            f"drawtext=fontfile='{FONT}':text='{line}':x=(w-text_w)/2:y={120 + i * 50}:"
            f"fontsize=40:fontcolor=white" for i, line in enumerate(lines))
        mover = ""
        if kind in ("starting_soon", "loading", "brb"):
            # a short looping animation (2 s period), like real overlay screens
            mover = (f",drawtext=fontfile='{FONT}':text='o':fontsize=30:fontcolor=yellow:"
                     f"x=260+mod(t\\,2)*60:y=300")
        video = f"color=c=0x1d2340:size={size}:rate=15,{draws}{mover}"
        audio = f"sine=frequency={220 + seed * 30}:duration={seconds}"
    subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "lavfi", "-t", str(seconds), "-i", video,
                    "-f", "lavfi", "-i", audio, "-t", str(seconds), "-c:v", "libx264",
                    "-preset", "ultrafast", "-g", "15", "-pix_fmt", "yuv420p", "-c:a", "aac",
                    "-ar", "44100", "-ac", "1", "-shortest", str(out)], check=True)


def make_sample(out, plan=None):
    """Synthetic stream file plus its ground-truth labels (list of spans)."""
    plan = plan or DEFAULT_PLAN
    out = Path(out)
    labels, t = [], 0.0
    with tempfile.TemporaryDirectory(dir=str(out.parent)) as temp:
        parts = []
        for i, (kind, seconds) in enumerate(plan):
            part = Path(temp) / f"{i:03d}.mp4"
            _segment(kind, seconds, part, i)
            parts.append(part)
            labels.append({"start": t, "end": t + seconds, "label": kind})
            t += seconds
        listing = Path(temp) / "list.txt"
        listing.write_text("".join(f"file '{p.as_posix()}'\n" for p in parts), encoding="utf-8")
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-f", "concat", "-safe", "0", "-i",
                        str(listing), "-c", "copy", str(out)], check=True)
    return labels


def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    lab = sub.add_parser("label")
    lab.add_argument("video")
    lab.add_argument("--every", type=float, default=10.0)
    lab.add_argument("--out")
    sc = sub.add_parser("score")
    sc.add_argument("video")
    sc.add_argument("labels")
    sc.add_argument("--no-ocr", action="store_true")
    sm = sub.add_parser("sample")
    sm.add_argument("out")
    args = ap.parse_args(argv)
    if args.cmd == "label":
        print(label_page(args.video, args.every, args.out))
    elif args.cmd == "score":
        result = scenes.analyze(args.video, use_ocr=not args.no_ocr)
        labels = json.loads(Path(args.labels).read_text(encoding="utf-8"))
        print(json.dumps({"scenes": result["scenes"], "score": score(result["scenes"], labels)},
                         indent=1))
    else:
        labels = make_sample(args.out)
        Path(args.out).with_suffix(".labels.json").write_text(json.dumps(labels, indent=1),
                                                              encoding="utf-8")
        print(args.out)


if __name__ == "__main__":
    main()
