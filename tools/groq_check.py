"""Phase 9: one real Groq Free transcription + judgement through the live-clip path.

    python tools/groq_check.py [--out D:/Clipping/Chopify/_phase9]

Uses synthetic Windows-TTS speech only (no recordings), one ~65-second audio
upload and one judgement - the same calls ``live_clipper`` makes for a
candidate (``LiveClipper._transcribe`` / ``_judge``). Runs with an isolated
config that copies your AI settings (Groq routes, free plan, ZDR
confirmation); keys come from .env. Free-only stays on; nothing paid is
possible. Prints and saves results without any key.
"""
import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from harness import HERE, isolated_env, speech  # noqa: E402


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(HERE.parent / "_phase9"))
    args = ap.parse_args()
    run = Path(args.out) / time.strftime("groq-%Y%m%d-%H%M%S")
    (run / "config").mkdir(parents=True)
    real = Path(os.environ.get("APPDATA", "")) / "Chopify" / "ai_settings.json"
    shutil.copy(real, run / "config" / "ai_settings.json")
    os.environ.update(isolated_env(run))
    sys.path.insert(0, str(HERE))
    import live_clipper
    from ai import gateway, policy, secrets
    settings = policy.load()
    wav = speech(run / "speech.wav", seconds=65)
    clipper = live_clipper.LiveClipper(scheduler=None)
    result = {"routes": settings["routes"], "groq_plan": settings["providers"]["groq"]["billing"],
              "free_only": settings["free_only"], "paid_calls": settings["paid_calls"],
              "zdr_confirmed": bool(settings["providers"]["groq"].get("zdr_confirmed_at"))}
    started = time.time()
    words, language = clipper._transcribe(wav, 1000.0, "groq-check")
    result["transcribe"] = {"seconds": round(time.time() - started, 1), "words": len(words),
                            "language": language, "first": words[0] if words else None,
                            "last_end": words[-1]["end"] if words else None,
                            "upload_bytes": None}
    item = {"start": 1015.0, "end": 1045.0, "peak": 1030.0}
    seg = clipper._refine({**item, "z": 6}, words)
    text = " ".join(w["word"] for w in words if seg["start"] <= w["start"] < seg["end"])
    started = time.time()
    score, judged = clipper._judge(run, seg, text, words, language)
    result["judge"] = {"seconds": round(time.time() - started, 1), "score": score,
                       "ai_judged": judged, "segment": seg}
    with sqlite3.connect(str(run / "config" / "ai_usage.db")) as db:
        result["ledger"] = db.execute(
            "SELECT provider, model, capability, status, audio_seconds, input_tokens, "
            "output_tokens, cost_usd, free, latency_ms FROM requests").fetchall()
    result["limits_seen"] = gateway.get().limits.get("groq")
    text_out = json.dumps(result, indent=1, default=str)
    key = secrets.get("groq") or "\0"
    assert key not in text_out
    (run / "groq_check.json").write_text(text_out, encoding="utf-8")
    print(text_out)


if __name__ == "__main__":
    main()
