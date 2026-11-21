"""Phase 3: automatic clip duration and context preservation.

Runs after selection (rule scores, AI judge, cap) and only moves the start and
end of the clips that were picked. Discovery, scoring and judging are not
touched, and ``duration_mode="fixed"`` leaves every clip exactly as before.

For each selected clip, in rank order:

1. Fit      - a clip far longer than suits its content type (for example a
              5-minute window of gameplay chatter) is cut down to its best
              self-contained stretch: complete sentences, a start that needs
              no earlier context, scored with the existing text scorer.
2. Trim     - drop dead edges: filler-only sentences at the start, "subscribe"
              outros and filler at the end.
3. Setup    - walk backwards while the clip opens on something that depends on
              earlier speech: a reference ("he", "that", "वो"), a connective
              ("but", "so", "toh", "लेकिन"), a reply ("yeah", "no", "haan"),
              or the answer to a question asked just before.
4. Payoff   - walk forwards while the clip ends on an unanswered question or
              mid-thought, or the very next line is a short reaction or the
              "because ..." that completes the thought.
5. Pad      - a little air before the first word and after the last word,
              never reaching into a neighbouring word.

Extensions stop at long pauses (a natural scene or topic change), at the
duration ceiling (max_len; 180 s for 9:16 Shorts), and before running into
another selected clip. Everything works from the transcript's sentences and
word timestamps; English, Hindi (Devanagari) and Hinglish cues are built in.
"""
import re

VERSION = 1
MODES = ("auto", "fixed")
DEFAULT_MODE = "auto"
SHORTS_MAX = 120.0                # Chopify's Shorts limit (YouTube allows 180)
SOFT_MAX_FACTOR = 1.5             # content band upper limit x this = "too long"
LEAD_PAD = 0.25                   # air before the first word (s)
TAIL_PAD = 0.60                   # air after the last word (s), keeps the beat
WORD_GUARD = 0.04                 # never cut closer than this to another word
MAX_SETUP_SENTENCES = 4
MAX_SETUP_SECONDS = 45.0
MAX_PAYOFF_SENTENCES = 3
MAX_PAYOFF_SECONDS = 20.0
TOPIC_PAUSE = 2.5                 # a pause this long separates scenes/topics
FOLLOW_GAP = 1.2                  # a reply/continuation follows within this
REACTION_SECONDS = 4.0
REACTION_WORDS = 4

# Openers that only make sense with what was said before.
REFERENCE_OPENERS = {
    # "this"/"these"/"there" are left out: "This is the secret...", "There's
    # a trick..." are normal standalone hooks.
    "that", "it", "he", "she", "they", "them", "him", "her", "those",
    "then", "also", "which", "who",
    # Hinglish (romanized Hindi)
    "woh", "wo", "ye", "yeh", "usne", "unhone", "uska", "unka", "isko", "usko",
    "waha", "wahan", "phir", "fir",
    # Hindi (Devanagari)
    "वो", "वह", "ये", "यह", "उसने", "उन्होंने", "उसका", "उनका", "इसको", "उसको",
    "वहाँ", "वहां", "फिर",
}
CONNECTIVE_OPENERS = {
    "and", "but", "so", "because", "cause", "or", "plus", "anyway",
    # not "to": "To be honest..." is a normal opener.
    "toh", "aur", "lekin", "magar", "isliye", "kyunki",
    "और", "लेकिन", "पर", "मगर", "तो", "इसलिए", "क्योंकि",
}
REPLY_OPENERS = {
    "yeah", "yes", "yep", "yup", "no", "nope", "nah", "exactly", "right",
    "true", "sure", "absolutely", "definitely",
    "haan", "ha", "nahi", "nahin", "bilkul", "sahi", "theek",
    "हाँ", "हां", "नहीं", "बिल्कुल", "सही", "ठीक",
}
CONTINUATION_OPENERS = {
    "because", "cause", "which", "kyunki", "isliye", "क्योंकि", "इसलिए",
}
CONTINUATION_PHRASES = ("that's why", "that is why", "and then", "which is why",
                        "is liye", "tabhi")
DANGLING_ENDERS = {"and", "but", "so", "because", "or", "the", "a", "to",
                   "aur", "lekin", "toh", "ki", "और", "लेकिन", "तो", "कि"}
FILLER_TOKENS = {"um", "uh", "uhh", "umm", "erm", "er", "hmm", "mmm", "mm",
                 "ah", "eh", "okay", "ok", "alright", "so", "like", "matlab",
                 "achha", "acha", "अच्छा", "मतलब"}
OUTRO_PHRASES = ("thanks for watching", "subscribe", "hit the bell",
                 "like and share", "see you next time", "see you in the next",
                 "like share", "bell icon", "सब्सक्राइब", "अगले वीडियो")
REACTION_CUES = ("haha", "lol", "no way", "oh my god", "omg", "what the",
                 "let's go", "wow", "whoa", "damn", "bro", "yaar", "arey",
                 "kya baat", "wah", "वाह", "अरे")
REACTION_OPENER_CUES = (" kidding ", " no way ", " oh my god ", " omg ",
                        " what the ", " are you serious ", " seriously ",
                        " oh shit ", " holy ", " wow ", " whoa ", " damn ",
                        " haha", " lol ", " arey ", " kya baat ", " वाह ", " अरे ")
TERMINAL_RE = re.compile(r"[.!?…।॥][\"')\]]?$")
_TOKEN_STRIP = re.compile(r"[^\w'ऀ-ॿ]")


# ------------------------------------------------------------- sentences

def tokens(text):
    out = []
    for raw in str(text or "").lower().split():
        token = _TOKEN_STRIP.sub("", raw.replace("’", "'"))
        if token:
            out.append(token)
    return out


def _text(sentence):
    return re.sub(r"\s+", " ", str(sentence.get("text") or "")).strip()


def terminal(sentence):
    return bool(TERMINAL_RE.search(_text(sentence)))


def is_question(sentence):
    return _text(sentence).endswith("?")


def gap_before(sentences, index):
    if index <= 0:
        return float("inf")
    return float(sentences[index]["start"]) - float(sentences[index - 1]["end"])


def needs_setup(sentences, index):
    """Why sentence ``index`` cannot open a clip on its own, or None."""
    words = tokens(_text(sentences[index]))
    if not words:
        return None
    first = words[0]
    # "That's" / "it's" / "he's" count as references too.
    base = first.split("'")[0]
    if first in REFERENCE_OPENERS or base in REFERENCE_OPENERS:
        return "opens with a reference to something earlier"
    if first in CONNECTIVE_OPENERS:
        return "opens mid-thought"
    raw = _text(sentences[index]).split()[0]
    if first in REPLY_OPENERS and (re.search(r"[,.!?।]$", raw) or len(words) <= 3):
        # "Yeah, ..." / "No." is a reply; "Right now I'm ..." is not.
        return "opens with a reply"
    low = " " + " ".join(words) + " "
    if len(words) <= 6 and any(cue in low for cue in REACTION_OPENER_CUES):
        # "You gotta be kidding." / "No way!" reacts to something earlier.
        return "opens with a reaction to something earlier"
    if (index > 0 and is_question(sentences[index - 1])
            and gap_before(sentences, index) <= FOLLOW_GAP):
        return "answers the question asked just before"
    return None


def _is_reaction(sentence):
    words = tokens(_text(sentence))
    seconds = float(sentence["end"]) - float(sentence["start"])
    if not words or seconds > REACTION_SECONDS:
        return False
    low = " " + " ".join(words) + " "
    text = _text(sentence)
    return (len(words) <= REACTION_WORDS or "!" in text
            or any(cue in low for cue in REACTION_CUES))


def needs_payoff(sentences, index):
    """Why the clip should not end at sentence ``index`` yet, or None."""
    sentence = sentences[index]
    words = tokens(_text(sentence))
    if index + 1 >= len(sentences):
        return None
    if gap_before(sentences, index + 1) > TOPIC_PAUSE:
        return None
    if is_question(sentence):
        return "ends on an unanswered question"
    if not terminal(sentence) or (words and words[-1] in DANGLING_ENDERS):
        return "ends mid-sentence"
    if gap_before(sentences, index + 1) <= FOLLOW_GAP:
        following = sentences[index + 1]
        nxt = tokens(_text(following))
        low = " " + " ".join(nxt) + " "
        if nxt and (nxt[0] in CONTINUATION_OPENERS
                    or any(" " + p + " " in low[:40] for p in CONTINUATION_PHRASES)):
            return "the thought continues"
        if _is_reaction(following):
            return "keeps the reaction"
    return None


def _dead_start(sentence):
    words = tokens(_text(sentence))
    return not words or all(word in FILLER_TOKENS for word in words)


def _dead_end(sentence):
    words = tokens(_text(sentence))
    low = " " + " ".join(words) + " "
    return (not words or all(word in FILLER_TOKENS for word in words)
            or any(phrase in low for phrase in OUTRO_PHRASES))


# ------------------------------------------------------------- limits

def ceiling(max_len, aspect=None):
    """The hard duration limit for one clip."""
    limit = float(max_len) if max_len else SHORTS_MAX * 10
    if aspect == "9:16":
        limit = min(limit, SHORTS_MAX)
    return limit


def soft_limit(band, preferred_len=None, hard=None):
    """Above this a clip is "too long for its content" and gets fitted."""
    upper = float(preferred_len) if preferred_len else float((band or (30, 120))[1])
    limit = upper * SOFT_MAX_FACTOR
    return min(limit, hard) if hard else limit


def locate(sentences, start, end, tolerance=0.35):
    """(first, last) sentence indices covering [start, end], or None."""
    first = last = None
    for index, sentence in enumerate(sentences):
        if first is None and float(sentence["end"]) > start + tolerance:
            first = index
        if float(sentence["start"]) < end - tolerance:
            last = index
    if first is None or last is None or last < first:
        return None
    return first, last


def _span(sentences, a, b):
    return float(sentences[b]["end"]) - float(sentences[a]["start"])


# ------------------------------------------------------------- refine one

def best_window(sentences, a, b, low, high, score_window):
    """Best self-contained sentence range inside [a, b] lasting low..high s."""
    best = None
    for strict in (True, False):
        for i in range(a, b + 1):
            if strict and needs_setup(sentences, i):
                continue
            for j in range(i, b + 1):
                span = _span(sentences, i, j)
                if span > high:
                    break
                if span < low or not terminal(sentences[j]) or is_question(sentences[j]):
                    continue
                if strict and j < b and needs_payoff(sentences, j):
                    continue
                score = score_window(i, j) if score_window else 0.0
                mid = (low + high) / 2.0
                key = (round(score, 4), -abs(span - mid), -i)
                if best is None or key > best[0]:
                    best = (key, i, j)
        if best is not None:
            return best[1], best[2]
    return None


def _blocked(sentences, index, occupied):
    sentence = sentences[index]
    start, end = float(sentence["start"]), float(sentence["end"])
    return any(start < other_end and end > other_start
               for other_start, other_end in occupied)


def refine_range(sentences, a, b, *, min_len, hard, soft, band_low,
                 occupied=(), score_window=None, accept_shorter=None):
    """New (a, b) plus a report of what changed and why."""
    report = {"setup_sentences": 0, "payoff_sentences": 0, "trimmed": 0,
              "shortened": False, "notes": []}
    min_len = float(min_len or 0)

    if _span(sentences, a, b) > soft:
        fitted = best_window(sentences, a, b, max(min_len, band_low), soft,
                             score_window)
        if fitted is not None and fitted != (a, b):
            before = _span(sentences, a, b)
            if accept_shorter is None or accept_shorter(*fitted):
                a, b = fitted
                report["shortened"] = True
                report["notes"].append(
                    f"shortened from {before:.0f}s to its best self-contained "
                    f"{_span(sentences, a, b):.0f}s")
            else:
                report["notes"].append(
                    f"kept the full {before:.0f}s: the AI judge rated the "
                    "shorter version lower")

    while a < b and _dead_start(sentences[a]) and _span(sentences, a + 1, b) >= min_len:
        a += 1
        report["trimmed"] += 1
    while (b > a and _dead_end(sentences[b]) and terminal(sentences[b - 1])
           and _span(sentences, a, b - 1) >= min_len):
        b -= 1
        report["trimmed"] += 1
    if report["trimmed"]:
        report["notes"].append(f"trimmed {report['trimmed']} filler/outro sentence(s)")

    origin = float(sentences[a]["start"])
    while report["setup_sentences"] < MAX_SETUP_SENTENCES and a > 0:
        reason = needs_setup(sentences, a)
        if not reason:
            break
        previous = a - 1
        if gap_before(sentences, a) > TOPIC_PAUSE:
            report["notes"].append(f"start {reason}, but a long pause comes before it")
            break
        if (_blocked(sentences, previous, occupied)
                or _span(sentences, previous, b) > hard
                or origin - float(sentences[previous]["start"]) > MAX_SETUP_SECONDS):
            report["notes"].append(f"start {reason}; no room to add more setup")
            break
        a = previous
        report["setup_sentences"] += 1
        report["notes"].append(f"added setup: the start {reason}")

    origin = float(sentences[b]["end"])
    while report["payoff_sentences"] < MAX_PAYOFF_SENTENCES and b + 1 < len(sentences):
        reason = needs_payoff(sentences, b)
        if not reason:
            break
        following = b + 1
        if (_blocked(sentences, following, occupied)
                or _span(sentences, a, following) > hard
                or float(sentences[following]["end"]) - origin > MAX_PAYOFF_SECONDS):
            report["notes"].append(f"end {reason}; no room to extend")
            break
        b = following
        report["payoff_sentences"] += 1
        report["notes"].append(f"added payoff: the end {reason}")
    return a, b, report


def pad_times(sentences, a, b, duration=None):
    """Clip start/end with a little air, never cutting into another word."""
    first_words = sentences[a].get("words") or []
    last_words = sentences[b].get("words") or []
    first_start = float(first_words[0]["start"] if first_words else sentences[a]["start"])
    last_end = float(last_words[-1]["end"] if last_words else sentences[b]["end"])
    start = first_start - LEAD_PAD
    if a > 0:
        previous_end = float(sentences[a - 1]["end"])
        start = max(start, min(first_start, previous_end + WORD_GUARD))
    end = last_end + TAIL_PAD
    if b + 1 < len(sentences):
        next_start = float(sentences[b + 1]["start"])
        end = min(end, max(last_end, next_start - WORD_GUARD))
    start = max(0.0, start)
    if duration:
        end = min(float(duration), end)
    return round(start, 2), round(end, 2)


# ------------------------------------------------------------- all clips

def refine_segments(segments, sentences, *, mode=DEFAULT_MODE, duration=None,
                    min_len=15, max_len=300, band=(30, 120), preferred_len=None,
                    aspect=None, score_window=None, make_hook=None,
                    verify_shorter=None):
    """Return (segments, stats). ``mode="fixed"`` returns them unchanged.

    ``verify_shorter(segment, first, last)`` may veto fitting a too-long clip
    to sentences first..last (for example by asking the AI judge whether the
    shorter version is still as good); None accepts every fit.
    """
    if mode not in MODES:
        raise ValueError("Duration mode must be auto or fixed.")
    hard = ceiling(max_len, aspect)
    soft = soft_limit(band, preferred_len, hard)
    stats = {"version": VERSION, "mode": mode, "max_allowed": hard,
             "too_long_above": round(soft, 1), "refined": 0, "setup_added": 0,
             "payoff_added": 0, "shortened": 0, "trimmed": 0, "unmatched": 0}
    durations = [float(s["end"]) - float(s["start"]) for s in segments]
    stats["duration_before"] = ([round(min(durations), 2), round(max(durations), 2)]
                                if durations else None)
    if mode == "fixed" or not segments or not sentences:
        if mode == "auto" and segments and not sentences:
            stats["unmatched"] = len(segments)
        stats["duration_after"] = stats["duration_before"]
        return [dict(s) for s in segments], stats

    band_low = float((band or (30, 120))[0])
    result = [dict(s) for s in segments]
    order = sorted(range(len(result)), key=lambda i: (
        result[i].get("rank") or 10 ** 6, -float(result[i].get("overall", 0)),
        float(result[i]["start"])))
    ranges = {i: (float(s["start"]), float(s["end"])) for i, s in enumerate(result)}
    for i in order:
        segment = result[i]
        original = (float(segment["start"]), float(segment["end"]))
        found = locate(sentences, *original)
        if found is None:
            stats["unmatched"] += 1
            continue
        occupied = [r for k, r in ranges.items() if k != i]
        accept = None
        if verify_shorter is not None:
            accept = (lambda first, last, seg=segment:
                      verify_shorter(seg, first, last))
        a, b, report = refine_range(
            sentences, found[0], found[1], min_len=min_len, hard=hard, soft=soft,
            band_low=band_low, occupied=occupied, score_window=score_window,
            accept_shorter=accept)
        start, end = pad_times(sentences, a, b, duration)
        for other_start, other_end in occupied:   # padding never overlaps
            if other_end <= original[0] + 1e-6:
                start = max(start, other_end)
            if other_start >= original[1] - 1e-6:
                end = min(end, other_start)
        content = (float(sentences[a]["start"]), float(sentences[b]["end"]))
        if content[1] - content[0] > hard + 1e-6:
            # No self-contained version fits: reject it, never cut mid-thought.
            segment["too_long"] = round(content[1] - content[0], 2)
        elif end - start > hard:                   # only padding is trimmed
            end = max(content[1], start + hard)
            start = max(start, end - hard)
        segment.update(start=start, end=end, duration=round(end - start, 2))
        if make_hook and (a, b) != found:
            segment["hook"] = make_hook(sentences[a].get("text", "")) or segment.get("hook")
        segment["boundaries"] = {
            "mode": "auto",
            "original": [round(original[0], 2), round(original[1], 2)],
            "setup_sentences": report["setup_sentences"],
            "payoff_sentences": report["payoff_sentences"],
            "trimmed": report["trimmed"],
            "shortened": report["shortened"],
            "notes": report["notes"],
        }
        ranges[i] = (start, end)
        stats["refined"] += 1
        stats["setup_added"] += bool(report["setup_sentences"])
        stats["payoff_added"] += bool(report["payoff_sentences"])
        stats["shortened"] += bool(report["shortened"])
        stats["trimmed"] += bool(report["trimmed"])
    durations = [float(s["end"]) - float(s["start"]) for s in result]
    stats["duration_after"] = [round(min(durations), 2), round(max(durations), 2)]
    return result, stats
