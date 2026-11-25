"""Story-based clip selection (clip mode "story").

Instead of scoring loud or keyword-dense windows, the AI judge reads the
whole transcript in order - in chunks of about five minutes, carrying a
running "story so far" summary so every chunk is understood in context - and
proposes complete narrative sequences: a challenge, an unexpected event, a
funny situation, a conflict, a reaction, a satisfying payoff. Each proposal is
a range of numbered transcript sentences with scores for hook, context,
middle, payoff, coherence and engagement.

Chopify then:
* checks the ranges against the real timestamps (sentence-aligned starts and
  ends), keeps clips between ``min_len`` and the 120 s Shorts limit
  (trimming from the front so the payoff is never cut);
* penalises dead air (long silences inside the clip); the share of time the
  scene classifier labelled as non-gameplay is recorded but does not lower
  the score while scene detection is in observation mode;
* removes overlapping and duplicate stories (time overlap or same text);
* selects adaptively: never below an absolute quality floor
  (``STORY_FLOOR``), otherwise everything within ``RELATIVE_MARGIN`` of the
  best story, up to the usual number of clips for the video's length -
  so one fixed threshold cannot reject every story of a decent video.

Judgements are cached in ``story_cache.json`` (per chunk, model and prompt
version), so selecting again costs no AI calls. When no AI judge is
available the caller falls back to the automatic scoring mode.
"""
import hashlib
import json
import re
import time
from pathlib import Path

import applog
import boundaries
import highlights

VERSION = 1
PROMPT_VERSION = 3
CHUNK_SECONDS = 300.0
OVERLAP_SECONDS = 60.0
MAX_SENTENCE_SECONDS = 20.0
STORY_FLOOR = 5.0
RELATIVE_MARGIN = 2.5
SHORTS_MAX = boundaries.SHORTS_MAX
TYPES = ("challenge", "unexpected", "funny", "conflict", "reaction", "payoff", "reveal",
         "skill", "story")
WEIGHTS = {"hook": 0.22, "context": 0.12, "middle": 0.14, "payoff": 0.24,
           "coherence": 0.16, "engagement": 0.12}
CACHE_FILE = "story_cache.json"

SYSTEM = """You are the story editor for a YouTube Shorts channel that clips gaming and
entertainment videos. Viewers swipe away in two seconds unless a clip hooks them, and
they only share clips that tell a complete little story.

From the numbered transcript sentences, pick the moments that work as a stand-alone
Short: a challenge, an unexpected event, a funny situation, a conflict, a reaction or a
satisfying payoff. For each story give the FIRST and LAST sentence numbers so that:
- the first sentence hooks a stranger and still gives enough context to follow;
- the middle builds the situation (no unrelated chatter, walking or repeated gameplay);
- the last sentence is the payoff or the reaction to it - never cut before the end;
- it lasts about 20 to 110 seconds (use the times shown).
Give each *_score from 0 to 10 (quality, not a sentence number; most material is 3-6,
8+ is rare). Return 0 to 4 stories
for this part of the video; return none rather than weak ones. Use only sentence
numbers that are shown. Also summarise what happens in this part in one or two
sentences (for context in later parts)."""

# Deliberately not "strict": Groq's strict mode refuses the whole answer
# (HTTP 400 "Failed to validate JSON") when one field misses the schema, so
# the schema goes into the prompt and Chopify validates and repairs answers.
SCHEMA = {
    "type": "object",
    "required": ["summary", "stories"],
    "properties": {
        "summary": {"type": "string"},
        "stories": {"type": "array", "items": {
            "type": "object",
            "required": ["title", "type", "first", "last", "hook_line", "why", "hook_score",
                         "context_score", "middle_score", "payoff_score", "coherence_score",
                         "engagement_score"],
            "properties": {
                "title": {"type": "string"},
                "type": {"type": "string", "description": " | ".join(TYPES)},
                "first": {"type": "integer", "description": "number of the first sentence"},
                "last": {"type": "integer", "description": "number of the last sentence"},
                "hook_line": {"type": "string"}, "why": {"type": "string"},
                "hook_score": {"type": "integer", "description": "0-10: how strongly the first line grabs a stranger"},
                "context_score": {"type": "integer", "description": "0-10: can a stranger follow it"},
                "middle_score": {"type": "integer", "description": "0-10: does the middle keep building"},
                "payoff_score": {"type": "integer", "description": "0-10: how satisfying the ending is"},
                "coherence_score": {"type": "integer", "description": "0-10: is it one complete story"},
                "engagement_score": {"type": "integer", "description": "0-10: would people watch and share it"}}}}}}


# --------------------------------------------------------------- transcript

def clean_words(words):
    """Strip spaces; drop immediate duplicates (same word, same start)."""
    out = []
    for w in words or []:
        text = str(w.get("word", "")).strip()
        if not text:
            continue
        if out and out[-1]["word"] == text and abs(out[-1]["start"] - float(w["start"])) < 0.05:
            continue
        out.append({"word": text, "start": float(w["start"]), "end": float(w["end"])})
    return out


def sentences(words, gap=1.2, max_seconds=MAX_SENTENCE_SECONDS):
    """Sentence list (start, end, text, words); very long ones are split."""
    import score_clips
    out = []
    for s in score_clips.sentenceize(clean_words(words), gap=gap):
        piece = []
        for w in s["words"]:
            if piece and w["end"] - piece[0]["start"] > max_seconds:
                out.append(piece)
                piece = []
            piece.append(w)
        if piece:
            out.append(piece)
    return [{"start": p[0]["start"], "end": p[-1]["end"],
             "text": " ".join(w["word"] for w in p), "words": p} for p in out]


def chunks(sents, size=CHUNK_SECONDS, overlap=OVERLAP_SECONDS):
    """[(first_index, last_index)] windows of ~size seconds that overlap."""
    if not sents:
        return []
    out, start = [], 0
    while start < len(sents):
        t0 = sents[start]["start"]
        end = start
        while end + 1 < len(sents) and sents[end + 1]["end"] - t0 <= size:
            end += 1
        out.append((start, end))
        if end >= len(sents) - 1:
            break
        nxt = end
        while nxt > start and sents[end]["end"] - sents[nxt]["start"] < overlap:
            nxt -= 1
        start = max(start + 1, nxt)
    return out


def _clock(t):
    t = int(t)
    return f"{t // 3600}:{t % 3600 // 60:02}:{t % 60:02}" if t >= 3600 else f"{t // 60}:{t % 60:02}"


def chunk_prompt(sents, first, last, title, so_far):
    lines = [f"[{i}] ({_clock(sents[i]['start'])}) {sents[i]['text']}"
             for i in range(first, last + 1)]
    context = " ".join(so_far[-6:]) or "This is the start of the video."
    return (f"Video title: {title or 'unknown'}\n"
            f"What happened before this part: {context}\n\n"
            f"Transcript sentences {first}-{last}:\n" + "\n".join(lines))


# ------------------------------------------------------------------ AI pass

def _load_cache(path):
    try:
        data = json.loads((path / CACHE_FILE).read_text(encoding="utf-8"))
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def _save_cache(path, cache):
    target = path / CACHE_FILE
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(cache, ensure_ascii=False), encoding="utf-8")
    temp.replace(target)


def judge_chunks(path, provider, sents, title, progress=None, sleep=time.sleep):
    """Run the AI pass over every chunk. Returns (proposals, warnings)."""
    import llm as llm_mod
    path = Path(path)
    cache = _load_cache(path)
    so_far, proposals, warnings = [], [], []
    parts = chunks(sents)
    for n, (first, last) in enumerate(parts, 1):
        prompt = chunk_prompt(sents, first, last, title, so_far)
        # Keyed on this part's own text (not the running summary): one failed
        # call must not invalidate every later part's cached answer.
        own = chunk_prompt(sents, first, last, title, [])
        key = hashlib.sha256(json.dumps([provider.label, PROMPT_VERSION, own]).encode(
            "utf-8")).hexdigest()[:24]
        result = cache.get(key)
        if result is None:
            for attempt in range(2):
                try:
                    if getattr(provider, "accepts_hints", False):
                        result = provider.chat_json(SYSTEM, prompt, schema=SCHEMA,
                                                    reasoning="low", max_output_tokens=4000)
                    else:
                        result = provider.chat_json(SYSTEM, prompt, schema=SCHEMA)
                    break
                except llm_mod.LLMError as exc:
                    if attempt:
                        warnings.append(f"Part {n} ({_clock(sents[first]['start'])}) was "
                                        f"not judged: {exc}")
                    else:
                        sleep(30)          # often a per-minute rate limit
            if isinstance(result, dict):
                cache[key] = result
                _save_cache(path, cache)
        if progress:
            progress(n, len(parts))
        if not isinstance(result, dict):
            continue
        so_far.append(f"[{_clock(sents[first]['start'])}] {str(result.get('summary', ''))[:300]}")
        for story in result.get("stories") or []:
            if isinstance(story, dict):
                proposals.append({**story, "chunk": [first, last]})
    return proposals, warnings


# -------------------------------------------------------------- candidates

def _score(parts):
    return round(sum(WEIGHTS[k] * max(0.0, min(10.0, float(parts.get(k) or 0)))
                     for k in WEIGHTS), 2)


def dead_air(words, start, end, gap=2.0):
    """Share of the clip spent in silences longer than ``gap`` seconds."""
    inside = [w for w in words if start <= w["start"] < end]
    if not inside:
        return 1.0
    silent = max(0.0, inside[0]["start"] - start) + max(0.0, end - inside[-1]["end"])
    for a, b in zip(inside, inside[1:]):
        if b["start"] - a["end"] > gap:
            silent += b["start"] - a["end"]
    return round(min(1.0, silent / max(1e-6, end - start)), 3)


def non_gameplay(scene_list, start, end):
    """Share of the clip labelled as a break screen (starting soon, BRB, menu...)."""
    if not scene_list:
        return 0.0
    import scenes
    bad = 0.0
    for s in scene_list:
        if s.get("label") in scenes.NON_GAMEPLAY:
            bad += max(0.0, min(end, s["end"]) - max(start, s["start"]))
    return round(bad / max(1e-6, end - start), 3)


def build(proposal, sents, words, scene_list, min_len=15.0, max_len=SHORTS_MAX,
          duration=None):
    """One validated candidate segment from an AI proposal, or None."""
    try:
        first, last = int(proposal.get("first", -1)), int(proposal.get("last", -1))
    except (TypeError, ValueError):
        return None
    lo, hi = proposal.get("chunk") or (0, len(sents) - 1)
    if not (lo <= first <= last <= hi) or last >= len(sents):
        return None
    limit = min(float(max_len or SHORTS_MAX), SHORTS_MAX)
    start, end = sents[first]["start"], sents[last]["end"]
    # Too long (padding included): keep the payoff, move the start forward
    # to the next sentence start.
    while (end + 0.35) - max(0.0, start - 0.15) > limit and first < last:
        first += 1
        start = sents[first]["start"]
    # Too short: add the following sentences (the reaction) when they exist.
    while end - start < min_len and last + 1 < len(sents) and \
            sents[last + 1]["end"] - start <= limit:
        last += 1
        end = sents[last]["end"]
    start = max(0.0, start - 0.15)
    end = end + 0.35
    if duration:
        end = min(end, float(duration))
    if not min_len <= end - start <= limit + 0.5:
        return None
    end = min(end, start + limit)
    parts = {}
    for k in WEIGHTS:
        try:
            value = float(proposal.get(f"{k}_score", proposal.get(k)))
        except (TypeError, ValueError):
            return None
        if not 0 <= value <= 10:
            return None              # not a 0-10 score (e.g. a sentence number): discard
        parts[k] = int(round(value))
    kind = str(proposal.get("type") or "").strip().lower()
    proposal = {**proposal, "type": kind if kind in TYPES else "story"}
    base = _score(parts)
    air = dead_air(words, start, end)
    breaks = non_gameplay(scene_list, start, end)
    # Scene labels are informational only: the classifier is still in
    # observation mode (unvalidated on real gameplay), so it never lowers a score.
    penalty = round(min(3.0, max(0.0, air - 0.15) * 6), 2)
    overall = round(max(0.0, base - penalty), 2)
    text = " ".join(s["text"] for s in sents[first:last + 1])
    title = re.sub(r"\s+", " ", str(proposal.get("title") or "")).strip()[:90]
    return {
        "start": round(start, 2), "end": round(end, 2), "duration": round(end - start, 2),
        "hook": title or " ".join(text.split()[:8]),
        "overall": overall, "raw_score": base,
        "reason": str(proposal.get("why") or "")[:400],
        "score_parts": {**{k: int(parts[k] or 0) for k in WEIGHTS},
                        "dead_air": air, "non_gameplay": breaks, "penalty": penalty},
        "judge": {"verdict": "keep", "llm_score": base, "summary": str(proposal.get("why") or "")[:400],
                  "scores": {k: int(parts[k] or 0) for k in WEIGHTS}, "mode": "story"},
        "story": {"type": proposal.get("type"), "title": title,
                  "hook_line": str(proposal.get("hook_line") or "")[:200],
                  "sentences": [first, last], "text": text[:2000]},
        "boundaries": {"mode": "story", "notes": ["whole sentences, payoff kept"]},
    }


def _overlap(a, b):
    inter = max(0.0, min(a["end"], b["end"]) - max(a["start"], b["start"]))
    shorter = min(a["end"] - a["start"], b["end"] - b["start"])
    return inter / max(1e-6, shorter)


def _jaccard(a, b):
    x = set(re.findall(r"\w+", a.lower()))
    y = set(re.findall(r"\w+", b.lower()))
    return len(x & y) / max(1, len(x | y))


def dedupe(candidates, overlap=0.5, text=0.6):
    """Best first; drop any candidate that overlaps or repeats a kept one."""
    kept, dropped = [], []
    for c in sorted(candidates, key=lambda c: -c["overall"]):
        twin = next((k for k in kept if _overlap(c, k) >= overlap
                     or _jaccard(c["story"]["text"], k["story"]["text"]) >= text), None)
        if twin:
            dropped.append({"start": c["start"], "end": c["end"], "duplicate_of": twin["start"]})
        else:
            kept.append(c)
    return kept, dropped


def choose(candidates, duration, max_clips=None, min_score=None):
    """Adaptive selection: floor, margin below the best, length-based cap."""
    if not candidates:
        return [], None
    best = candidates[0]["overall"]
    wanted = STORY_FLOOR if min_score is None else float(min_score)
    threshold = round(max(STORY_FLOOR, min(wanted, best - RELATIVE_MARGIN)), 2)
    cap = highlights.quality_cap(duration, max_clips)
    return [c for c in candidates if c["overall"] >= threshold][:cap], threshold


# -------------------------------------------------------------- entry point

def select(path, provider, title="", min_len=15, max_len=SHORTS_MAX, max_clips=None,
           min_score=None, progress=None):
    """Write segments.json / selection.json for clip mode "story".

    Returns the selection stats. Raises ValueError when there is no
    transcript to read.
    """
    path = Path(path)
    data = json.loads((path / "transcript.json").read_text(encoding="utf-8"))
    words = clean_words(data.get("words"))
    if not words:
        raise ValueError("Story mode needs a transcript with words.")
    sents = sentences(words)
    try:
        scene_list = json.loads((path / "scenes.json").read_text(encoding="utf-8"))["scenes"]
    except (OSError, ValueError, KeyError):
        scene_list = []
    started = time.time()
    proposals, warnings = judge_chunks(path, provider, sents, title, progress)
    duration = float(data.get("duration") or words[-1]["end"])
    built = [c for c in (build(p, sents, words, scene_list, min_len, max_len, duration)
                         for p in proposals) if c]
    ranked, duplicates = dedupe(built)
    chosen, threshold = choose(ranked, duration, max_clips, min_score)
    chosen.sort(key=lambda c: c["start"])
    for rank, c in enumerate(sorted(chosen, key=lambda c: -c["overall"]), 1):
        c["rank"] = rank
    stats = {
        "mode": "story", "version": VERSION, "judge": provider.label,
        "source_duration": round(duration, 2), "sentences": len(sents),
        "parts": len(chunks(sents)), "proposals": len(proposals), "valid": len(built),
        "candidates": len(ranked), "duplicates_removed": len(duplicates),
        "passing": len(chosen), "threshold": threshold, "floor": STORY_FLOOR,
        "score_min": min((c["overall"] for c in ranked), default=None),
        "score_max": max((c["overall"] for c in ranked), default=None),
        "passing_score_min": min((c["overall"] for c in chosen), default=None),
        "passing_score_max": max((c["overall"] for c in chosen), default=None),
        "duration_min": min((c["duration"] for c in chosen), default=None),
        "duration_max": max((c["duration"] for c in chosen), default=None),
        "max_duration": SHORTS_MAX, "warnings": warnings,
        "seconds": round(time.time() - started, 1),
        "ranked": [{k: c[k] for k in ("start", "end", "overall", "hook")} for c in ranked[:30]]}
    (path / "segments.json").write_text(json.dumps(chosen, indent=1, ensure_ascii=False),
                                        encoding="utf-8")
    (path / "selection.json").write_text(json.dumps(stats, indent=1, ensure_ascii=False),
                                         encoding="utf-8")
    applog.info("story.select", f"Story mode: {len(chosen)} of {len(ranked)} stories selected",
                proposals=len(proposals), threshold=threshold, warnings=len(warnings) or None)
    print(f"Story mode: {len(proposals)} stories proposed, {len(ranked)} distinct, "
          f"{len(chosen)} selected (threshold {threshold}).", flush=True)
    return stats


def available(options):
    """The project's AI judge for story mode, or None."""
    if not options.get("llm"):
        return None
    import llm as llm_mod
    try:
        provider = llm_mod.get_provider(options.get("llm_provider") or "ollama", options["llm"])
    except ValueError:
        return None
    ok, _ = provider.available()
    return provider if ok else None
