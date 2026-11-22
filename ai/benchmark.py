"""Benchmark AI routes on Chopify's own material (read-only for projects).

    python -m ai.benchmark transcription --audio AUDIO --reference REF \\
        [--start 600 --seconds 300] --routes local:medium groq:whisper-large-v3-turbo
    python -m ai.benchmark judging --project PROJECT --baseline local:qwen2.5:3b \\
        --routes groq:openai/gpt-oss-20b [--limit 20]
    python -m ai.benchmark synthetic --routes gemini:gemini-3.5-flash-lite

REF is a transcript.json (words with start/end) or a YouTube json3 caption
file already stored in a project. A route that cannot run (no key, privacy,
free-only, quota) is reported as unavailable with the reason - nothing is
guessed. Reports (JSON + Markdown) go to --out (default D:\\Clipping\\Chopify\\_bench);
project folders are never written to.
"""
import argparse
import difflib
import json
import statistics
import subprocess
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent.parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

from ai.base import REAL, SYNTHETIC, AIError  # noqa: E402

DEFAULT_OUT = Path(r"D:\Clipping\Chopify\_bench")


# ---------------------------------------------------------------- metrics

def normalize_words(words):
    import subtitles
    return [t for t in (subtitles.normalize(w) for w in words) if t]


def edit_distance(a, b):
    previous = list(range(len(b) + 1))
    for i, x in enumerate(a, 1):
        current = [i]
        for j, y in enumerate(b, 1):
            current.append(min(previous[j] + 1, current[j - 1] + 1, previous[j - 1] + (x != y)))
        previous = current
    return previous[-1]


def wer(reference, hypothesis):
    ref, hyp = normalize_words(reference), normalize_words(hypothesis)
    return round(edit_distance(ref, hyp) / max(1, len(ref)), 4)


def cer(reference, hypothesis):
    ref = "".join(normalize_words(reference))
    hyp = "".join(normalize_words(hypothesis))
    return round(edit_distance(list(ref), list(hyp)) / max(1, len(ref)), 4)


def timing_offset(reference, hypothesis):
    """Median |start difference| (s) of matched words, and how many matched."""
    import subtitles
    a = [(subtitles.normalize(w["word"]), w) for w in reference]
    b = [(subtitles.normalize(w["word"]), w) for w in hypothesis]
    a, b = [x for x in a if x[0]], [x for x in b if x[0]]
    matcher = difflib.SequenceMatcher(None, [t for t, _ in a], [t for t, _ in b], autojunk=False)
    diffs = [abs(float(a[m.a + k][1]["start"]) - float(b[m.b + k][1]["start"]))
             for m in matcher.get_matching_blocks() for k in range(m.size)]
    return (round(statistics.median(diffs), 3) if diffs else None), len(diffs)


def script_share(words):
    letters = [c for w in words for c in str(w) if c.isalpha()]
    if not letters:
        return {"devanagari": 0.0, "latin": 0.0}
    deva = sum(1 for c in letters if "\u0900" <= c <= "\u097f")
    latin = sum(1 for c in letters if c.isascii())
    return {"devanagari": round(deva / len(letters), 3), "latin": round(latin / len(letters), 3)}


def spearman(xs, ys):
    def ranks(values):
        order = sorted(range(len(values)), key=lambda i: values[i])
        out = [0.0] * len(values)
        for rank, i in enumerate(order):
            out[i] = float(rank)
        return out
    if len(xs) < 3:
        return None
    rx, ry = ranks(xs), ranks(ys)
    mx, my = statistics.mean(rx), statistics.mean(ry)
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    den = (sum((a - mx) ** 2 for a in rx) * sum((b - my) ** 2 for b in ry)) ** 0.5
    return round(num / den, 3) if den else None


# --------------------------------------------------------------- material

def load_reference(path, start, seconds):
    path = Path(path)
    if path.suffix == ".json3":
        import subtitles
        words, _ = subtitles.parse_file(path)
    else:
        words = json.loads(path.read_text(encoding="utf-8")).get("words") or []
    end = start + seconds
    return [{"word": str(w["word"]).strip(), "start": float(w["start"]) - start,
             "end": float(w["end"]) - start} for w in words if start <= float(w["start"]) < end]


def cut_sample(audio, start, seconds, out_dir):
    out_dir.mkdir(parents=True, exist_ok=True)
    sample = out_dir / f"sample_{Path(audio).stem}_{int(start)}_{int(seconds)}.wav"
    if not sample.exists():
        subprocess.run(["ffmpeg", "-v", "error", "-y", "-ss", str(start), "-t", str(seconds),
                        "-i", str(audio), "-vn", "-ac", "1", "-ar", "16000", str(sample)],
                       check=True)
    return sample


# ------------------------------------------------------------------ suites

def transcription(gateway, audio, reference, routes, start=0.0, seconds=300.0,
                  out=DEFAULT_OUT, language=None, data_kind=REAL):
    sample = cut_sample(audio, start, seconds, Path(out) / "samples")
    ref = load_reference(reference, start, seconds)
    rows = []
    for route in routes:
        row = {"suite": "transcription", "route": route, "sample": sample.name,
               "reference": Path(reference).name, "reference_words": len(ref)}
        started = time.time()
        try:
            result = gateway.transcribe(str(sample), language_hint=language, route=route,
                                        data_kind=data_kind)
        except AIError as exc:
            row.update(status="unavailable", reason=f"{exc.kind}: {exc}")
            rows.append(row)
            continue
        words = [{"word": w.word, "start": w.start, "end": w.end}
                 for s in result.segments for w in s.words]
        offset, matched = timing_offset(ref, words)
        usage = result.usage
        row.update(status="ok", words=len(words), language=result.language,
                   wer=wer([w["word"] for w in ref], [w["word"] for w in words]),
                   cer=cer([w["word"] for w in ref], [w["word"] for w in words]),
                   timing_median_s=offset, timing_matched=matched,
                   script=script_share(w["word"] for w in words),
                   latency_s=round(time.time() - started, 1),
                   realtime_factor=round((time.time() - started) / max(seconds, 1), 3),
                   cost_usd=round(usage.cost_usd, 6) if usage else None,
                   cost_per_source_hour=round(usage.cost_usd / seconds * 3600, 4)
                   if usage else None, free=bool(usage.free) if usage else None)
        rows.append(row)
    return rows


def judging(gateway, project, baseline, routes, limit=20, data_kind=REAL):
    import highlights
    import llm
    project = Path(project)
    segments = json.loads((project / "segments.json").read_text(encoding="utf-8"))
    transcript = json.loads((project / "transcript.json").read_text(encoding="utf-8"))
    words = transcript.get("words") or []
    items = []
    for seg in segments[:limit]:
        text = " ".join(str(w["word"]).strip() for w in words
                        if seg["start"] <= w["start"] < seg["end"])
        if text:
            items.append(({"start": seg["start"], "end": seg["end"]}, text))
    scores, rows = {}, []
    for route in [baseline] + list(routes):
        provider_name, _, model = route.partition(":")
        provider = (llm.get_provider("ollama", model) if provider_name == "local"
                    else llm.get_provider("gateway", route))
        ok, reason = provider.available()
        row = {"suite": "judging", "route": route, "clips": len(items)}
        if not ok:
            row.update(status="unavailable", reason=reason)
            rows.append(row)
            continue
        results, valid, started = [], 0, time.time()
        for item, text in items:
            try:
                judgment = highlights.validate_judgment(highlights._judge_clip(
                    provider, item, text, highlights.context_before(words, item["start"]),
                    transcript.get("language") or ""))
            except Exception:                       # noqa: BLE001 - counted as invalid
                judgment = None
            valid += judgment is not None
            results.append(judgment)
        scores[route] = results
        row.update(status="ok", json_valid=round(valid / max(1, len(items)), 3),
                   latency_s_per_clip=round((time.time() - started) / max(1, len(items)), 2))
        rows.append(row)
    base = scores.get(baseline)
    for row in rows:
        mine = scores.get(row["route"])
        if row["route"] == baseline or not base or not mine:
            continue
        pairs = [(a, b) for a, b in zip(base, mine) if a and b]
        row["verdict_agreement"] = round(sum(a["verdict"] == b["verdict"] for a, b in pairs)
                                         / max(1, len(pairs)), 3)
        row["score_spearman"] = spearman([highlights.llm_score(a) for a, _ in pairs],
                                         [highlights.llm_score(b) for _, b in pairs])
    return rows


SYNTHETIC_CLIPS = [
    "Okay chat, last lap, I am one corner behind and the tyres are gone... and I take him on "
    "the inside! Yes! First place by half a second, I cannot believe that just happened.",
    "So yaar ye mission teen baar fail ho chuka hai, ab bas ek try aur. Wait wait, helicopter "
    "aa gaya... aur boom, ho gaya! Finally!",
    "Welcome back everyone, today we are just driving around and checking the map, nothing "
    "special, let me find a parking spot first.",
]


def synthetic(gateway, routes):
    """Structured-output reliability on synthetic, made-up commentary only."""
    import highlights
    schema = {"type": "object", "additionalProperties": False,
              "required": list(highlights.RUBRIC_WEIGHTS) + ["verdict", "reason"],
              "properties": {**{k: {"type": "number"} for k in highlights.RUBRIC_WEIGHTS},
                             "verdict": {"type": "string", "enum": list(highlights.VERDICTS)},
                             "reason": {"type": "string"}}}
    rows = []
    for route in routes:
        valid, latencies, error = 0, [], None
        for text in SYNTHETIC_CLIPS:
            started = time.time()
            try:
                result = gateway.generate("Rate this gaming clip for YouTube Shorts 0-10 per "
                                          "rubric key. Reply as JSON.", text, schema=schema,
                                          route=route, data_kind=SYNTHETIC)
                valid += highlights.validate_judgment(result.data) is not None
                latencies.append(time.time() - started)
            except AIError as exc:
                error = f"{exc.kind}: {exc}"
                break
        rows.append({"suite": "synthetic", "route": route,
                     "status": "ok" if error is None else "unavailable", "reason": error,
                     "json_valid": round(valid / len(SYNTHETIC_CLIPS), 3) if error is None else None,
                     "avg_latency_s": round(statistics.mean(latencies), 2) if latencies else None})
    return rows


def write_report(rows, out, title):
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    (out / f"{title}-{stamp}.json").write_text(json.dumps(rows, ensure_ascii=False, indent=1),
                                               encoding="utf-8")
    keys = sorted({k for r in rows for k in r})
    lines = [f"# {title} ({stamp})", "", "| " + " | ".join(keys) + " |",
             "|" + "---|" * len(keys)]
    lines += ["| " + " | ".join(str(r.get(k, "")) for k in keys) + " |" for r in rows]
    (out / f"{title}-{stamp}.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    try:
        from ai.usage import Ledger
        ledger = Ledger()
        for row in rows:
            ledger.add_benchmark(row.get("suite"), row["route"].partition(":")[0],
                                 row["route"].partition(":")[2], row.get("sample", ""),
                                 row, row.get("status", ""), row.get("reason") or "")
    except Exception:                              # noqa: BLE001 - report files are enough
        pass
    return out / f"{title}-{stamp}.md"


def main(argv=None):
    from ai import gateway
    ap = argparse.ArgumentParser(description="Benchmark Chopify AI routes.")
    sub = ap.add_subparsers(dest="suite", required=True)
    t = sub.add_parser("transcription")
    t.add_argument("--audio", required=True)
    t.add_argument("--reference", required=True)
    t.add_argument("--start", type=float, default=0.0)
    t.add_argument("--seconds", type=float, default=300.0)
    t.add_argument("--language")
    t.add_argument("--routes", nargs="+", required=True)
    j = sub.add_parser("judging")
    j.add_argument("--project", required=True)
    j.add_argument("--baseline", default="local:qwen2.5:3b")
    j.add_argument("--routes", nargs="*", default=[])
    j.add_argument("--limit", type=int, default=20)
    s = sub.add_parser("synthetic")
    s.add_argument("--routes", nargs="+", required=True)
    for p in (t, j, s):
        p.add_argument("--out", default=str(DEFAULT_OUT))
        p.add_argument("--title", default=None)
    args = ap.parse_args(argv)
    gw = gateway.get()
    if args.suite == "transcription":
        rows = transcription(gw, args.audio, args.reference, args.routes, args.start,
                             args.seconds, args.out, args.language)
    elif args.suite == "judging":
        rows = judging(gw, args.project, args.baseline, args.routes, args.limit)
    else:
        rows = synthetic(gw, args.routes)
    report = write_report(rows, args.out, args.title or args.suite)
    print(json.dumps(rows, ensure_ascii=False, indent=1))
    print(f"Report: {report}")


if __name__ == "__main__":
    main()
