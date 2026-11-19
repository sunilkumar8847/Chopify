"""Validate editable transcripts and export clip-relative SubRip captions."""
import copy
import math
from pathlib import Path

from score_clips import sentenceize


def update_transcript(data, words):
    if not isinstance(words, list) or not words:
        raise ValueError("Provide at least one timed word.")
    duration = float(data["duration"])
    clean = []
    previous = 0.0
    for item in words:
        text = str(item.get("word", "")).strip()
        start, end = float(item["start"]), float(item["end"])
        if (not text or len(text) > 500 or "\n" in text or "\r" in text
                or not math.isfinite(start) or not math.isfinite(end)
                or start < previous - 0.001 or end <= start or end > duration + 0.001):
            raise ValueError("Words must have text and ordered, non-overlapping times within the video.")
        entry = {"word": text, "start": start, "end": end}
        for flag in ("hidden", "emphasis"):
            if flag in item:
                if not isinstance(item[flag], bool):
                    raise ValueError("Word flags must be true or false.")
                if item[flag]:
                    entry[flag] = True
        clean.append(entry)
        previous = end
    result = copy.deepcopy(data)
    result["words"] = clean
    result["segments"] = [{k: s[k] for k in ("start", "end", "text")}
                          for s in sentenceize(clean)]
    return result


def srt_time(seconds):
    ms = max(0, round(seconds * 1000))
    hours, ms = divmod(ms, 3600000)
    minutes, ms = divmod(ms, 60000)
    seconds, ms = divmod(ms, 1000)
    return f"{hours:02}:{minutes:02}:{seconds:02},{ms:03}"


SENTENCE_END = (".", "!", "?", "…", "।", "॥")
CUE_PAUSE = 1.0   # seconds of silence that always start a new caption cue


def group_words(selected, group_size, max_chars=None, pause=CUE_PAUSE):
    """Split words into caption cues (shared by the burned-in ASS and the SRT).

    A cue ends after ``group_size`` words, before a word that would push it
    past ``max_chars`` (so vertical captions stay on one line), after the end
    of a sentence, and before a pause longer than ``pause`` seconds, so a
    caption never lingers over silence or mixes two sentences.
    """
    groups, current = [], []
    for word in selected:
        if current:
            text = " ".join(w["word"].strip() for w in current + [word])
            gap = float(word["start"]) - float(current[-1]["end"])
            if (len(current) >= group_size
                    or (max_chars and len(text) > max_chars)
                    or current[-1]["word"].strip().endswith(SENTENCE_END)
                    or gap > pause):
                groups.append(current)
                current = []
        current.append(word)
    if current:
        groups.append(current)
    return groups


def write_srt(words, start, end, path, group_size=8, max_chars=None):
    if isinstance(group_size, bool) or not isinstance(group_size, int) or not 1 <= group_size <= 100:
        raise ValueError("Caption group size must be an integer from 1 to 100.")
    selected = [w for w in words
                if not w.get("hidden") and w["end"] > start and w["start"] < end]
    blocks = []
    for group in group_words(selected, group_size, max_chars):
        a = max(start, group[0]["start"]) - start
        b = min(end, group[-1]["end"]) - start
        text = " ".join(w["word"].strip() for w in group)
        blocks.append(f"{len(blocks) + 1}\n{srt_time(a)} --> {srt_time(b)}\n{text}\n")
    Path(path).write_text("\n".join(blocks), encoding="utf-8")
    return Path(path)
