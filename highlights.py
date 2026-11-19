"""Phase 2: pick fewer, better clips (quality over quantity).

Runs after the existing rule-based scoring and overlap dedupe:

1. Shortlist  - about 30-60 of the strongest rule-scored candidates, spread
                across the whole video (the best ones always get in, then
                every part of the timeline gets a fair share).
2. Judge      - a local LLM (Ollama, Qwen 2.5 3B by default) scores each
                shortlisted clip 0-10 for hook, payoff, emotion, standalone
                context and shareability, in batches, as validated JSON.
                English, Hindi (Devanagari) and Hinglish are all judged on
                meaning. Results are cached per clip text, model and prompt.
3. Combine    - final = 30% rule score (as a percentile within this video,
                so it is language-neutral) + 70% LLM score. A "skip" verdict
                caps the clip at 4.0.
4. De-dupe    - drop clips that tell the same story twice (embeddings when an
                embedding model is installed, otherwise character n-grams,
                which work for Devanagari, Latin and mixed text).
5. Cap        - a duration-based ceiling (3 to 12 clips) unless the user set
                max_clips, so a long video never yields 50 clips.

If the LLM is unavailable or returns unusable output, the shortlisted clips
keep their rule scores (the previous behaviour) and steps 4-5 still apply.
"""
import hashlib
import json
import math
import re
from pathlib import Path

import llm as llm_mod

VERSION = 1
PROMPT_VERSION = 1
CACHE_FILE = "highlights_cache.json"
SHORTLIST_MIN, SHORTLIST_DEFAULT, SHORTLIST_MAX = 30, 40, 60
BATCH_SIZE = 5
TEXT_LIMIT = 1600
CONTEXT_SECONDS = 25.0
CONTEXT_LIMIT = 400
RUBRIC_WEIGHTS = {"hook": 0.25, "payoff": 0.20, "emotion": 0.20,
                  "standalone": 0.20, "shareability": 0.15}
RULE_WEIGHT, LLM_WEIGHT = 0.30, 0.70
SKIP_CAP = 4.0
VERDICTS = ("keep", "maybe", "skip")
EMBED_DUPLICATE = 0.92
LEXICAL_DUPLICATE = 0.60
CAP_MIN, CAP_MAX, MINUTES_PER_CLIP = 3, 12, 8.0

SYSTEM_PROMPT = (
    "You are an expert short-form video editor for YouTube Shorts, Instagram "
    "Reels and TikTok. You decide whether a transcript excerpt would make a "
    "strong standalone short clip.\n"
    "Transcripts can be English, Hindi in Devanagari script, or Hinglish "
    "(Hindi written in Latin letters mixed with English). Judge the meaning, "
    "never the language or spelling. Transcripts are automatic and may "
    "contain recognition errors.\n"
    "Score every clip from 0 to 10 on:\n"
    "- hook: do the first seconds grab attention?\n"
    "- payoff: does it land a punchline, answer, reveal or emotional peak?\n"
    "- emotion: humour, surprise, tension, inspiration or outrage.\n"
    "- standalone: understandable without the rest of the video? Penalize "
    "clips that start mid-thought or depend on earlier context.\n"
    "- shareability: would viewers share, comment or rewatch it?\n"
    "Be strict. Most clips are average (4-6). Reserve 8 or more for clips you "
    "would actually publish. verdict is keep, maybe or skip.\n"
    "Reply with JSON only, one entry per clip id, reason in one short "
    "English sentence."
)

RESPONSE_SCHEMA = {
    "type": "object",
    "properties": {"clips": {"type": "array", "items": {
        "type": "object",
        "properties": {
            "id": {"type": "integer"},
            **{key: {"type": "number"} for key in RUBRIC_WEIGHTS},
            "verdict": {"type": "string", "enum": list(VERDICTS)},
            "reason": {"type": "string"},
        },
        "required": ["id", *RUBRIC_WEIGHTS, "verdict", "reason"],
    }}},
    "required": ["clips"],
}


# ------------------------------------------------------------- helpers

def _key(item):
    return (round(float(item["start"]), 2), round(float(item["end"]), 2))


def rule_percentiles(candidates):
    """0-10 rank of each candidate's rule score within this video."""
    scores = sorted(float(item["overall"]) for item in candidates)
    if not scores:
        return {}
    result = {}
    for item in candidates:
        value = float(item["overall"])
        below = sum(1 for score in scores if score < value)
        equal = sum(1 for score in scores if score == value)
        result[_key(item)] = round(10.0 * (below + 0.5 * equal) / len(scores), 3)
    return result


def shortlist(candidates, size=SHORTLIST_DEFAULT, duration=None):
    """Strongest candidates, spread across the whole timeline.

    Half the slots go to the best scores anywhere; the rest are filled round
    robin from equal time buckets so no part of a long video is ignored.
    """
    size = max(1, int(size))
    ranked = sorted(candidates, key=lambda c: (-float(c["overall"]),
                                               float(c["start"])))
    if len(ranked) <= size:
        return sorted(ranked, key=lambda c: float(c["start"]))
    end = float(duration or 0) or max(float(c["end"]) for c in ranked)
    chosen, taken = [], set()

    def take(item):
        if _key(item) not in taken:
            taken.add(_key(item))
            chosen.append(item)

    for item in ranked[: size // 2]:
        take(item)
    buckets = max(1, math.ceil(size / 4))
    queues = [[] for _ in range(buckets)]
    for item in ranked:  # already best-first inside every bucket
        index = min(buckets - 1, int(float(item["start"]) / end * buckets))
        queues[index].append(item)
    while len(chosen) < size and any(queues):
        for queue in queues:
            while queue and _key(queue[0]) in taken:
                queue.pop(0)
            if queue and len(chosen) < size:
                take(queue.pop(0))
    return sorted(chosen, key=lambda c: float(c["start"]))


def shortlist_size(count):
    """About 30-60 clips to judge, never more than exist."""
    return max(min(count, SHORTLIST_MIN), min(SHORTLIST_MAX, max(
        SHORTLIST_DEFAULT, count // 10), count))


def quality_cap(duration, max_clips=None):
    """How many clips to keep at most. User-set max_clips always wins."""
    if max_clips is not None:
        return int(max_clips)
    minutes = max(0.0, float(duration or 0) / 60.0)
    return int(max(CAP_MIN, min(CAP_MAX, math.ceil(minutes / MINUTES_PER_CLIP))))


def context_before(words, start, seconds=CONTEXT_SECONDS, limit=CONTEXT_LIMIT):
    """Transcript just before a clip, so the judge can rate standalone-ness."""
    text = " ".join(str(w.get("word", "")).strip() for w in words or []
                    if start - seconds <= float(w.get("start", 0)) < start)
    return text[-limit:].strip()


def clip_text(item, words):
    text = str(item.get("text") or "").strip()
    if not text:
        text = " ".join(str(w.get("word", "")).strip() for w in words or []
                        if item["start"] <= float(w.get("start", 0)) < item["end"])
    return text[:TEXT_LIMIT]


def _cache_key(label, text, context):
    return hashlib.sha256(json.dumps(
        [PROMPT_VERSION, label, text, context], ensure_ascii=False
    ).encode("utf-8")).hexdigest()


def load_cache(workdir):
    path = Path(workdir) / CACHE_FILE
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("version") == VERSION and isinstance(data.get("entries"), dict):
            return data["entries"]
    except (OSError, ValueError, AttributeError):
        pass
    return {}


def save_cache(workdir, entries):
    path = Path(workdir) / CACHE_FILE
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({"version": VERSION, "entries": entries},
                               ensure_ascii=False), encoding="utf-8")
    temp.replace(path)


def validate_judgment(entry):
    """A clean judgment dict, or None when the model's entry is unusable."""
    if not isinstance(entry, dict):
        return None
    result = {}
    for key in RUBRIC_WEIGHTS:
        value = entry.get(key)
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            return None
        if not math.isfinite(value) or not 0 <= value <= 10:
            return None
        result[key] = round(float(value), 2)
    verdict = str(entry.get("verdict", "")).strip().lower()
    if verdict not in VERDICTS:
        return None
    result["verdict"] = verdict
    result["reason"] = re.sub(r"\s+", " ", str(entry.get("reason") or "")).strip()[:160]
    return result


def llm_score(judgment):
    score = sum(RUBRIC_WEIGHTS[key] * judgment[key] for key in RUBRIC_WEIGHTS)
    if judgment["verdict"] == "skip":
        score = min(score, SKIP_CAP)
    return round(score, 2)


def build_prompt(batch, language):
    clips = [{"id": index, "seconds": round(item["end"] - item["start"]),
              "context_before": context, "transcript": text}
             for index, (item, text, context) in enumerate(batch)]
    return ("Detected language: " + (language or "unknown") + "\n"
            + json.dumps({"clips": clips}, ensure_ascii=False) + "\n"
            + 'Return {"clips":[{"id":0,"hook":0,"payoff":0,"emotion":0,'
              '"standalone":0,"shareability":0,"verdict":"keep|maybe|skip",'
              '"reason":"..."}]} with one entry for every id above.')


def _judge_batch(provider, batch, language):
    """{index: judgment} for one batch; one retry on unusable output."""
    for _ in range(2):
        try:
            raw = provider.chat_json(SYSTEM_PROMPT, build_prompt(batch, language),
                                     RESPONSE_SCHEMA)
        except llm_mod.LLMError:
            continue
        entries = raw.get("clips") if isinstance(raw, dict) else raw
        if not isinstance(entries, list):
            continue
        found = {}
        for entry in entries:
            index = entry.get("id") if isinstance(entry, dict) else None
            if isinstance(index, bool) or not isinstance(index, int):
                continue
            if 0 <= index < len(batch) and index not in found:
                judgment = validate_judgment(entry)
                if judgment is not None:
                    found[index] = judgment
        if found:
            return found
    return {}


# ----------------------------------------------------- near duplicates

def _normalize(text):
    return re.sub(r"\s+", " ", re.sub(r"[^\w\sऀ-ॿ]", " ",
                                      str(text).lower())).strip()


def _ngrams(text, n=4):
    text = f" {_normalize(text)} "
    counts = {}
    for i in range(max(0, len(text) - n + 1)):
        gram = text[i:i + n]
        counts[gram] = counts.get(gram, 0) + 1
    return counts


def _tfidf(texts):
    """Character 4-gram vectors weighted so patterns shared by most clips
    (function words like "the", "you", "है") count little and distinctive
    shared phrases count a lot. Smoothed IDF keeps two-clip videos working.

    Measured on a real 67-minute English gameplay video: unrelated clips
    scored <= 0.35; rewordings of the same story (English, Hindi, Hinglish)
    scored >= 0.69. LEXICAL_DUPLICATE (0.60) sits between them.
    """
    grams = [_ngrams(text) for text in texts]
    count = len(grams)
    df = {}
    for gram in grams:
        for key in gram:
            df[key] = df.get(key, 0) + 1
    return [{key: value * (math.log((1 + count) / (1 + df[key])) + 1.0)
             for key, value in gram.items()} for gram in grams]


def _overlaps(first, second):
    return (min(float(first["end"]), float(second["end"]))
            > max(float(first["start"]), float(second["start"])))


def _cosine(a, b):
    if isinstance(a, dict):
        dot = sum(value * b.get(key, 0) for key, value in a.items())
        na = math.sqrt(sum(v * v for v in a.values()))
        nb = math.sqrt(sum(v * v for v in b.values()))
    else:
        dot = sum(x * y for x, y in zip(a, b))
        na = math.sqrt(sum(x * x for x in a))
        nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def remove_near_duplicates(records, texts, provider=None, embed_model=None):
    """Keep the best of each group of clips that say the same thing.

    ``records`` must be sorted best-first. Returns (kept, dropped, method).
    """
    vectors, method, threshold = None, "lexical", LEXICAL_DUPLICATE
    if provider is not None and embed_model and len(records) > 1:
        try:
            vectors = provider.embed([texts[_key(r)] or " " for r in records],
                                     model=embed_model)
            method, threshold = "embeddings", EMBED_DUPLICATE
        except llm_mod.LLMError:
            vectors = None
    if vectors is None:
        vectors = _tfidf([texts[_key(r)] for r in records])
    kept, kept_vectors, dropped = [], [], []
    for record, vector in zip(records, vectors):
        # Clips that overlap in time share words by definition; the overlap
        # rule upstream handles them. This step catches the same story told
        # at different times.
        twin = next((other for other, other_vector in zip(kept, kept_vectors)
                     if not _overlaps(record, other)
                     and _cosine(vector, other_vector) >= threshold), None)
        if twin is not None:
            dropped.append({"start": record["start"], "end": record["end"],
                            "duplicate_of": [twin["start"], twin["end"]]})
            continue
        kept.append(record)
        kept_vectors.append(vector)
    return kept, dropped, method


# ------------------------------------------------------------------ run

def _print(message):
    print(message, flush=True)


def run(workdir, candidates, discovery, transcript, model=None, provider_name=None,
        host=None, embed_model=None, max_clips=None, provider=None, log=_print):
    """Shortlist, judge, combine, de-duplicate and cap. Returns (records, info).

    Records keep every existing field; ``overall`` becomes the final score and
    a ``judge`` block explains it. ``info`` goes into selection.json.
    """
    words = (transcript or {}).get("words") or []
    duration = (discovery or {}).get("source_duration") or (transcript or {}).get("duration")
    language = (transcript or {}).get("language") or ""
    info = {"version": VERSION, "judge": None, "status": "off", "reason": "",
            "shortlisted": 0, "judged": 0, "from_cache": 0,
            "near_duplicates": 0, "dedupe_method": "lexical",
            "cap": quality_cap(duration, max_clips)}
    if not candidates:
        return [], info

    percentiles = rule_percentiles(candidates)
    picked = shortlist(candidates, shortlist_size(len(candidates)), duration)
    info["shortlisted"] = len(picked)
    texts = {_key(item): clip_text(item, words) for item in picked}

    if model and provider is None:
        provider = llm_mod.get_provider(provider_name, model, host)
    judgments = {}
    if provider is not None:
        info["judge"] = provider.label
        ok, reason = provider.available()
        if not ok:
            info.update(status="unavailable", reason=reason)
            log(f"AI judge unavailable ({reason}) -> rule-based scores")
        else:
            cache = load_cache(workdir)
            todo = []
            for item in picked:
                context = context_before(words, float(item["start"]))
                key = _cache_key(provider.label, texts[_key(item)], context)
                if key in cache and validate_judgment(cache[key]):
                    judgments[_key(item)] = cache[key]
                    info["from_cache"] += 1
                else:
                    todo.append((item, texts[_key(item)], context, key))
            log(f"AI judge {provider.label}: {len(picked)} shortlisted, "
                f"{info['from_cache']} cached, {len(todo)} to judge")
            for start in range(0, len(todo), BATCH_SIZE):
                batch = todo[start:start + BATCH_SIZE]
                found = _judge_batch(provider, [(i, t, c) for i, t, c, _ in batch],
                                     language)
                for index, judgment in found.items():
                    item, _, _, key = batch[index]
                    judgments[_key(item)] = judgment
                    cache[key] = judgment
                if found:
                    save_cache(workdir, cache)
            info["judged"] = len(judgments)
            if not judgments:
                info.update(status="invalid",
                            reason="The model returned no usable judgments.")
                log("AI judge returned no usable output -> rule-based scores")
            elif len(judgments) < len(picked):
                info.update(status="partial",
                            reason=f"{len(picked) - len(judgments)} clip(s) "
                                   "kept their rule score.")
            else:
                info["status"] = "ok"

    records = []
    for item in picked:
        record = dict(item)
        key = _key(item)
        rule_score = float(item["overall"])
        judge = {"rule_score": round(rule_score, 2),
                 "rule_percentile": percentiles.get(key)}
        judgment = judgments.get(key)
        if judgment is not None:
            score = llm_score(judgment)
            final = RULE_WEIGHT * percentiles.get(key, 0.0) + LLM_WEIGHT * score
            if judgment["verdict"] == "skip":
                final = min(final, SKIP_CAP)
            judge.update(model=info["judge"], llm_score=score,
                         scores={k: judgment[k] for k in RUBRIC_WEIGHTS},
                         verdict=judgment["verdict"], reason=judgment["reason"])
            record["overall"] = round(max(0.0, min(10.0, final)), 2)
            if judgment["reason"]:
                record["rule_reason"] = item.get("reason", "")
                record["reason"] = judgment["reason"]
        record["judge"] = judge
        records.append(record)

    records.sort(key=lambda r: (-float(r["overall"]), float(r["start"])))
    provider_for_embed = provider if info["status"] in {"ok", "partial"} else None
    records, dropped, method = remove_near_duplicates(
        records, texts, provider_for_embed, embed_model)
    info.update(near_duplicates=len(dropped), dedupe_method=method,
                duplicates=dropped[:20])
    if dropped:
        log(f"Removed {len(dropped)} near-duplicate clip(s) ({method})")
    records.sort(key=lambda r: (float(r["start"]), float(r["end"])))
    return records, info
