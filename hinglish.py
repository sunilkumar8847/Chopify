"""Global output-language policy: Hinglish in Roman (Latin) script only.

Whisper writes Hindi speech in Devanagari. Chopify keeps that original
transcript for internal processing (scoring, AI judging), but everything it
*generates* for people - burned-in captions, SRT files, clip titles, hooks,
summaries, YouTube titles, descriptions, hashtags and tags - is written in
Roman script:

* Hindi       -> natural Hinglish ("आज मैंने पहली बार" -> "aaj maine pehli baar")
* Hinglish    -> unchanged (already Roman)
* English     -> unchanged
* English words that Whisper spelled in Devanagari (गेमप्ले, वीडियो) are
  restored to English spelling (gameplay, video).

How a transcript is converted, word by word (so every word keeps its exact
Whisper timestamps):

1. ``romanize_word`` - deterministic transliteration (inherent-vowel deletion,
   nukta, nasalisation, Hinglish endings) plus dictionaries of common Hindi
   words and Devanagari-spelled English words. Always returns Roman text.
2. Optional LLM refinement per sentence: the model suggests better spellings
   for the same words. A suggestion is accepted only if it is pure Roman
   script *and* sounds like the original word (consonant skeleton), so the
   model can fix "gemple" -> "gameplay" but can never translate or drop words.
3. ``assert_roman`` - the final gate: no Devanagari character ever leaves.

Results are cached per sentence in ``hinglish_cache.json`` (atomic, locked).
"""
import difflib
import unicodedata
import hashlib
import json
import re
from pathlib import Path

import jsoncache
import llm as llm_mod

VERSION = 1            # cache format
POLICY_VERSION = 1     # bump to regenerate converted captions/metadata
CACHE_FILE = "hinglish_cache.json"
DISPLAY_FILE = "transcript.display.json"
CHUNK_WORDS = 14       # words per LLM request
SIMILARITY = 0.6

DEVANAGARI_RE = re.compile(r"[ऀ-ॿ꣠-ꣿ᳐-᳿]")

CONSONANTS = {
    "क": "k", "ख": "kh", "ग": "g", "घ": "gh", "ङ": "n",
    "च": "ch", "छ": "chh", "ज": "j", "झ": "jh", "ञ": "n",
    "ट": "t", "ठ": "th", "ड": "d", "ढ": "dh", "ण": "n",
    "त": "t", "थ": "th", "द": "d", "ध": "dh", "न": "n", "ऩ": "n",
    "प": "p", "फ": "ph", "ब": "b", "भ": "bh", "म": "m",
    "य": "y", "र": "r", "ऱ": "r", "ल": "l", "ळ": "l", "ऴ": "l", "व": "v",
    "श": "sh", "ष": "sh", "स": "s", "ह": "h",
    # precomposed nukta letters
    "क़": "q", "ख़": "kh", "ग़": "gh", "ज़": "z", "ड़": "d", "ढ़": "dh",
    "फ़": "f", "य़": "y",
}
NUKTA_FORMS = {"क": "q", "ख": "kh", "ग": "gh", "ज": "z", "ड": "d",
               "ढ": "dh", "फ": "f", "य": "y"}
NUKTA = "़"
VIRAMA = "्"
ANUSVARA, CANDRABINDU, VISARGA = "ं", "ँ", "ः"
INDEPENDENT = {
    "अ": "a", "आ": "aa", "इ": "i", "ई": "ee", "उ": "u", "ऊ": "oo",
    "ऋ": "ri", "ॠ": "ri", "ऌ": "li", "ए": "e", "ऐ": "ai", "ओ": "o",
    "औ": "au", "ऍ": "e", "ऑ": "o", "ऎ": "e", "ऒ": "o", "ॲ": "e",
}
SIGNS = {
    "ा": "aa", "ि": "i", "ी": "ee", "ु": "u",
    "ू": "oo", "ृ": "ri", "ॄ": "ri", "ॅ": "e",
    "ॆ": "e", "े": "e", "ै": "ai", "ॉ": "o",
    "ॊ": "o", "ो": "o", "ौ": "au", "ॢ": "li",
    "ॣ": "li", "ॎ": "e", "ॏ": "aw", "ॕ": "e",
    "ॖ": "u", "ॗ": "oo",
}
OTHER = {"।": ".", "॥": ".", "ऽ": "", "ॐ": "om",
         "॰": ".", "ॱ": ""}
DIGITS = {chr(0x0966 + i): str(i) for i in range(10)}
INHERENT = "a?"           # marker: inherent vowel, may be deleted
LABIALS = ("p", "b", "m", "f")

# Common Hindi words: everyday Hinglish spellings beat literal ones.
COMMON = {
    "है": "hai", "हैं": "hain", "हे": "hai", "था": "tha", "थी": "thi", "थे": "the",
    "हूँ": "hoon", "हूं": "hoon", "हो": "ho", "हुआ": "hua", "हुई": "hui", "हुए": "hue",
    "नहीं": "nahi", "नही": "nahi", "ना": "na", "न": "na", "मत": "mat",
    "में": "mein", "मे": "mein", "मैं": "main", "मै": "main", "मैंने": "maine",
    "हम": "hum", "हमें": "humein", "हमने": "humne", "तुम": "tum", "तू": "tu",
    "आप": "aap", "आपको": "aapko", "आपने": "aapne", "मुझे": "mujhe", "मुझको": "mujhko",
    "तुझे": "tujhe", "उसे": "use", "उसको": "usko", "उसने": "usne", "उनको": "unko",
    "मेरा": "mera", "मेरी": "meri", "मेरे": "mere", "तेरा": "tera", "तेरी": "teri",
    "तेरे": "tere", "हमारा": "hamara", "हमारी": "hamari", "हमारे": "hamare",
    "अपना": "apna", "अपनी": "apni", "अपने": "apne", "उसका": "uska", "उसकी": "uski",
    "उसके": "uske", "इसका": "iska", "इसकी": "iski", "इसके": "iske", "इसको": "isko",
    "यह": "yeh", "ये": "ye", "वह": "woh", "वो": "wo", "वे": "woh", "यहाँ": "yahan",
    "यहां": "yahan", "वहाँ": "wahan", "वहां": "wahan", "कहाँ": "kahan", "कहां": "kahan",
    "क्या": "kya", "क्यों": "kyun", "क्यूँ": "kyun", "क्यूं": "kyun", "कैसे": "kaise",
    "कैसा": "kaisa", "कैसी": "kaisi", "कौन": "kaun", "कब": "kab", "कितना": "kitna",
    "कितने": "kitne", "कितनी": "kitni", "कोई": "koi", "कुछ": "kuch", "सब": "sab",
    "सभी": "sabhi", "और": "aur", "या": "ya", "लेकिन": "lekin", "पर": "par",
    "पे": "pe", "क्योंकि": "kyunki", "क्यूंकि": "kyunki", "क्योकि": "kyunki",
    "तो": "toh", "भी": "bhi", "ही": "hi", "का": "ka", "की": "ki", "के": "ke",
    "को": "ko", "से": "se", "ने": "ne", "कि": "ki", "जो": "jo", "जब": "jab",
    "तब": "tab", "अब": "ab", "फिर": "phir", "अभी": "abhi", "कभी": "kabhi",
    "आज": "aaj", "कल": "kal", "बहुत": "bahut", "बहोत": "bahut", "ज़्यादा": "zyada",
    "ज्यादा": "zyada", "थोड़ा": "thoda", "थोड़ी": "thodi", "अच्छा": "accha",
    "अच्छी": "acchi", "अच्छे": "acche", "बढ़िया": "badhiya", "बढ़्या": "badhiya",
    "ठीक": "theek", "सही": "sahi", "गलत": "galat", "पहली": "pehli", "पहला": "pehla",
    "पहले": "pehle", "बार": "baar", "भाई": "bhai", "भाया": "bhaya", "यार": "yaar",
    "दोस्त": "dost", "दोस्तों": "doston", "लोग": "log", "लोगों": "logon",
    "मज़ा": "maza", "मजा": "maza", "मज़े": "maze", "मजे": "maze", "खेला": "khela",
    "खेल": "khel", "खेलते": "khelte", "देखो": "dekho", "देखते": "dekhte",
    "देखा": "dekha", "चलो": "chalo", "चल": "chal", "रहा": "raha", "रही": "rahi",
    "रहे": "rahe", "गया": "gaya", "गई": "gayi", "गए": "gaye", "गये": "gaye",
    "करो": "karo", "करना": "karna", "करते": "karte", "किया": "kiya",
    "दिया": "diya", "लिया": "liya", "होगा": "hoga", "होगी": "hogi", "सकता": "sakta",
    "सकते": "sakte", "चाहिए": "chahiye", "चाहिये": "chahiye", "मतलब": "matlab",
    "वाला": "wala", "वाली": "wali", "वाले": "wale", "साथ": "saath", "बात": "baat",
    "हाँ": "haan", "हां": "haan", "जी": "ji", "अरे": "arre", "ओए": "oye",
    "बस": "bas", "एक": "ek", "दो": "do", "तीन": "teen", "चार": "chaar",
    "पांच": "paanch", "पाँच": "paanch", "हज़ार": "hazaar", "जगह": "jagah",
    "घर": "ghar", "गाड़ी": "gaadi", "गाडी": "gaadi", "पैसे": "paise", "पैसा": "paisa",
    "दुनिया": "duniya", "ज़िन्दगी": "zindagi", "जिंदगी": "zindagi", "सच": "sach",
    "शुरू": "shuru", "खत्म": "khatam", "ख़त्म": "khatam", "इंतज़ार": "intezaar",
    "इंतजार": "intezaar", "स्वागत": "swagat", "धन्यवाद": "dhanyavaad",
    "शुक्रिया": "shukriya", "प्यार": "pyaar", "दिल": "dil", "वाह": "wah",
    "सबसे": "sabse", "इसलिए": "isliye", "इसलिये": "isliye", "वैसे": "waise",
    "ऐसे": "aise", "ऐसा": "aisa", "ऐसी": "aisi", "जैसे": "jaise", "उधर": "udhar",
    "इधर": "idhar", "ऊपर": "upar", "नीचे": "neeche", "अंदर": "andar", "बाहर": "bahar",
}

# English words that Whisper spells in Devanagari (gaming / creator talk).
ENGLISH = {
    "गेम": "game", "गेम्स": "games", "गेमप्ले": "gameplay", "गेमिंग": "gaming",
    "वीडियो": "video", "वीडियोस": "videos", "विडियो": "video", "चैनल": "channel",
    "लाइक": "like", "शेयर": "share", "सब्सक्राइब": "subscribe", "कमेंट": "comment",
    "कमेंट्स": "comments", "मिशन": "mission", "मिशंस": "missions", "पुलिस": "police",
    "कार": "car", "बाइक": "bike", "गन": "gun", "प्लेन": "plane", "सिटी": "city",
    "वाइस": "vice", "स्टोरी": "story", "स्टार्ट": "start", "स्टाट": "start",
    "लेट्स": "let's", "गो": "go", "गाइस": "guys", "गाईस": "guys", "गैस": "guys",
    "ओके": "okay", "ओक": "okay", "सॉरी": "sorry", "थैंक": "thank", "थैंक्यू": "thank you",
    "थैंक्स": "thanks", "हेलो": "hello", "हेलब": "help", "हेल्प": "help",
    "वेलकम": "welcome", "बैक": "back", "ऑलराइट": "alright", "आल्राइट": "alright",
    "आर्राइट": "alright", "सीरीज": "series", "सीरीस": "series", "पार्ट": "part",
    "एपिसोड": "episode", "फर्स्ट": "first", "फॉर्स्ट": "first", "लास्ट": "last",
    "नेक्स्ट": "next", "मैप": "map", "मार्क": "mark", "प्रॉब्लम": "problem",
    "प्रोब्लम": "problem", "क्रेजी": "crazy", "क्रेज़ी": "crazy", "फिजिक्स": "physics",
    "फिसिक्स": "physics", "ड्राइविंग": "driving", "ड्राइव": "drive", "हाईवे": "highway",
    "स्ट्रेट": "straight", "लेफ्ट": "left", "राइट": "right", "टर्न": "turn",
    "बॉस": "boss", "किल": "kill", "किल्स": "kills", "हेडशॉट": "headshot",
    "लूट": "loot", "प्लेयर": "player", "प्लेयर्स": "players", "टीम": "team",
    "लाइव": "live", "स्ट्रीम": "stream", "चैट": "chat", "फैन": "fan", "फैंस": "fans",
    "ग्राफिक्स": "graphics", "डिटेल": "detail", "डिटेल्स": "details",
    "इंपॉर्टेंट": "important", "इंपूर्टेंट": "important", "इम्पोर्टेंट": "important",
    "रैंडम": "random", "रेंडमली": "randomly", "रैंडमली": "randomly",
    "इवेंट": "event", "इवेंट्स": "events", "एक्चुअली": "actually",
    "आक्छुली": "actually", "बेसिकली": "basically", "सीरियसली": "seriously",
    "ऑब्वियसली": "obviously", "एक्सपेक्ट": "expect", "एक्साइटेड": "excited",
    "एन्जॉय": "enjoy", "इंजोय": "enjoy", "इंजोई": "enjoy", "इंजॉय": "enjoy",
    "इंजोए": "enjoy", "फ्रेंड": "friend", "फ्रेंड्स": "friends", "मनी": "money",
    "शॉप": "shop", "स्टोर": "store", "हाउस": "house", "रूम": "room", "डोर": "door",
    "फोन": "phone", "फ़ोन": "phone", "मोबाइल": "mobile", "नंबर": "number",
    "सेट": "set", "सेटिंग्स": "settings", "मोड": "mode", "लेवल": "level",
    "हेल्थ": "health", "हीलिंग": "healing", "हील": "heal", "आर्मर": "armor",
    "वेपन": "weapon", "वेपन्स": "weapons", "शूट": "shoot", "जंप": "jump",
    "रन": "run", "स्पीड": "speed", "फास्ट": "fast", "स्लो": "slow", "कूल": "cool",
    "नाइस": "nice", "ऑसम": "awesome", "वाओ": "wow", "वाउ": "wow", "ओह": "oh",
    "नो": "no", "यस": "yes", "बाय": "bye", "प्लीज": "please", "प्लीज़": "please",
    "प्रेस": "press", "प्रैस": "press", "बटन": "button", "कंट्रोल": "control",
    "कंट्रोलर": "controller", "टाइम": "time", "रियल": "real", "रियली": "really",
    "ट्राय": "try", "ट्राई": "try", "ट्राइ": "try", "चीट": "cheat", "चीट्स": "cheats",
    "क्रिएटर": "creator", "रिएक्शन": "reaction", "शॉर्ट्स": "shorts",
    "हाइलाइट": "highlight", "क्लिप": "clip", "क्लिप्स": "clips", "चिप्स": "chips",
    "पॉपकॉर्न": "popcorn", "स्नैक्स": "snacks", "पिक्चर": "picture",
    "पिच्चर": "picture", "जर्नी": "journey", "जॉर्नी": "journey",
}

_SKELETON_STEPS = [("ph", "f"), ("ch", "C"), ("sh", "S"), ("th", "t"),
                   ("dh", "d"), ("kh", "k"), ("gh", "g"), ("bh", "b"),
                   ("jh", "j"), ("ck", "k"), ("c", "k"), ("q", "k"),
                   ("w", "v"), ("z", "j"), ("x", "ks")]


# ------------------------------------------------------------ detection

def has_devanagari(text):
    return bool(DEVANAGARI_RE.search(str(text or "")))


def assert_roman(text, what="text"):
    """The policy gate: raise if Devanagari is present."""
    if has_devanagari(text):
        raise ValueError(f"{what} contains Devanagari; the output policy is "
                         "Hinglish in Roman script.")
    return text


# ------------------------------------------------------- transliteration

def _units(word):
    """Split Devanagari into [consonant, vowel, nasal, extra] units."""
    units = []
    i = 0
    while i < len(word):
        ch = word[i]
        nxt = word[i + 1] if i + 1 < len(word) else ""
        if ch in CONSONANTS:
            roman = CONSONANTS[ch]
            if nxt == NUKTA:
                roman = NUKTA_FORMS.get(ch, roman)
                i += 1
            units.append([roman, INHERENT, False, ""])
        elif ch in SIGNS and units and units[-1][1] == INHERENT:
            units[-1][1] = SIGNS[ch]
        elif ch == VIRAMA and units:
            units[-1][1] = ""
        elif ch in INDEPENDENT:
            units.append(["", INDEPENDENT[ch], False, ""])
        elif ch in (ANUSVARA, CANDRABINDU):
            if units:
                units[-1][2] = True
            else:
                units.append(["", "", True, ""])
        elif ch == VISARGA and units:
            units[-1][3] += "h"
        elif ch in DIGITS:
            units.append(["", "", False, DIGITS[ch]])
        elif ch in OTHER:
            units.append(["", "", False, OTHER[ch]])
        elif ch == NUKTA or DEVANAGARI_RE.match(ch):
            pass                                  # stray marks: drop
        else:
            units.append(["", "", False, ch])
        i += 1
    return units


def _delete_schwas(units):
    letters = [u for u in units if u[0] or u[1]]
    if len(letters) <= 1:
        for u in letters:
            if u[1] == INHERENT:
                u[1] = "a"
        return
    # final inherent vowel goes, unless it follows a consonant cluster
    last = letters[-1]
    if last[1] == INHERENT:
        cluster = len(letters) >= 2 and letters[-2][0] and letters[-2][1] == ""
        last[1] = "a" if cluster else ""
    # medial: C(a) between a vowel and a consonant+vowel loses its 'a'
    for k in range(len(letters) - 2, 0, -1):
        unit, before, after = letters[k], letters[k - 1], letters[k + 1]
        if (unit[1] == INHERENT and unit[0] and before[1] != ""
                and after[0] and after[1]):
            unit[1] = ""
    for u in letters:
        if u[1] == INHERENT:
            u[1] = "a"


def _transliterate(word):
    units = _units(word)
    _delete_schwas(units)
    letters = [i for i, u in enumerate(units) if u[0] or u[1]]
    last_letter = letters[-1] if letters else -1
    out = []
    for index, (cons, vowel, nasal, extra) in enumerate(units):
        final = index == last_letter
        if vowel == "aa" and final and (cons or len(letters) > 1):
            vowel = "a"                       # raha, tha, kya
        elif vowel == "ee" and final:
            vowel = "i"                       # bhi, nahi
        elif vowel == "oo" and final and cons:
            vowel = "u"                       # tu
        if extra == "h" and (cons or vowel) and not final:
            extra = ""                        # medial visarga: dukh
        text = cons + vowel
        if nasal:
            following = next((units[j][0] for j in range(index + 1, len(units))
                              if units[j][0]), "")
            text += "m" if following.startswith(LABIALS) else "n"
        out.append(text + extra)
    roman = "".join(out)
    roman = roman.replace("chchh", "cch").replace("chchh", "cch")
    return roman


def has_foreign(text):
    return strip_foreign(text) != str(text or "")


def romanize_word(word):
    """One Devanagari token (may carry punctuation) -> Roman Hinglish."""
    word = str(word or "")
    if not has_devanagari(word):
        return strip_foreign(word) if has_foreign(word) else word
    match = re.match(r"^([^ऀ-ॿ]*)(.*?)([^ऀ-ॿ]*)$", word, re.S)
    lead, core, tail = match.groups() if match else ("", word, "")
    parts = re.split(r"([।॥])", core)
    pieces = []
    for part in parts:
        if not part:
            continue
        bare = part.replace("‍", "").replace("‌", "")
        if bare in COMMON:
            pieces.append(COMMON[bare])
        elif bare in ENGLISH:
            pieces.append(ENGLISH[bare])
        else:
            pieces.append(_transliterate(bare))
    roman = lead + "".join(pieces) + tail
    roman = DEVANAGARI_RE.sub("", roman)   # nothing unknown slips through
    return strip_foreign(roman)


def strip_foreign(text):
    """Remove letters of non-Latin scripts (Whisper sometimes hallucinates
    a Japanese or Cyrillic word into Hindi speech). Latin letters, digits,
    punctuation and emoji stay; other scripts' digits become ASCII digits."""
    out = []
    drop_marks = False
    for ch in str(text or ""):
        category = unicodedata.category(ch)
        if category.startswith("L"):
            latin = unicodedata.name(ch, "").startswith("LATIN") or ch.isascii()
            drop_marks = not latin
            if latin:
                out.append(ch)
        elif category.startswith("M"):
            if not drop_marks:
                out.append(ch)
        elif category == "Nd" and not ch.isascii():
            out.append(str(unicodedata.digit(ch, 0)))
            drop_marks = False
        else:
            out.append(ch)
            drop_marks = False
    return "".join(out)


def romanize(text):
    """Any text -> Roman script, keeping spaces, punctuation and English."""
    return re.sub(r"\S+", lambda m: romanize_word(m.group(0)), str(text or ""))


# ------------------------------------------------- LLM-assisted spelling

def skeleton(word):
    """Consonant skeleton for 'does it sound like the same word?'."""
    text = re.sub(r"[^a-z]", "", str(word).lower())
    for old, new in _SKELETON_STEPS:
        text = text.replace(old, new)
    text = re.sub(r"[aeiouyh]", "", text)
    return re.sub(r"(.)\1+", r"\1", text)


def sounds_alike(a, b):
    sa, sb = skeleton(a), skeleton(b)
    if sa == sb:
        return True
    if not sa or not sb:
        return False
    return difflib.SequenceMatcher(None, sa, sb).ratio() >= SIMILARITY


SYSTEM_PROMPT = (
    "You correct the spelling of Hinglish: Hindi speech typed in Roman "
    "letters, WhatsApp style. The words come from an automatic "
    "transliteration, so English words may be spelled by sound. Return the "
    "corrected spelling of every word: same number of words, same order, one "
    "output per input. Never translate, never add, drop or merge words, keep "
    "punctuation, Latin letters only. Unsure? Return the word unchanged. "
    "Reply with JSON only."
)
SCHEMA = {"type": "object",
          "properties": {"words": {"type": "array", "items": {"type": "string"}}},
          "required": ["words"]}


def _clean_suggestion(suggestion, rough):
    """Accept a model spelling only if it is the same word, in Roman script."""
    if not isinstance(suggestion, str):
        return rough
    suggestion = suggestion.strip()
    if (not suggestion or " " in suggestion.strip(" ") and " " not in rough
            or has_devanagari(suggestion) or len(suggestion) > 3 * len(rough) + 6
            or not re.search(r"[A-Za-z]", suggestion)):
        return rough
    if not sounds_alike(suggestion, rough):
        return rough
    # keep the original punctuation around the word
    lead = re.match(r"^[^\w']*", rough).group(0)
    tail = re.search(r"[^\w']*$", rough).group(0)
    core = suggestion.strip(".,!?;:…\"'()[]") or suggestion
    return lead + core + tail


class Converter:
    """Converts transcript words / text to Roman Hinglish, with caching.

    ``provider`` is an llm.Provider (or None for rules only). ``workdir`` is
    the project folder holding the cache (None: in-memory only).
    """

    def __init__(self, workdir=None, provider=None, log=None):
        self.workdir = Path(workdir) if workdir else None
        self.provider = provider
        self.log = log or (lambda message: None)
        self.memory = {}
        self.stats = {"chunks": 0, "llm": 0, "cached": 0, "rules": 0,
                      "rejected_words": 0}
        self._available = None

    @property
    def label(self):
        return self.provider.label if self.provider is not None else "rules"

    def _cache_path(self):
        return self.workdir / CACHE_FILE if self.workdir else None

    def _llm_ok(self):
        if self.provider is None:
            return False
        if self._available is None:
            try:
                self._available = bool(self.provider.available()[0])
            except Exception:  # noqa: BLE001 - provider trouble = rules only
                self._available = False
            if not self._available:
                self.log("Hinglish: AI spelling unavailable -> rule-based transliteration")
        return self._available

    def _key(self, tokens):
        return hashlib.sha256(json.dumps(
            [POLICY_VERSION, self.label, tokens], ensure_ascii=False
        ).encode("utf-8")).hexdigest()

    def convert_tokens(self, tokens):
        """Same-length list of Roman tokens for a list of tokens."""
        tokens = [str(t) for t in tokens]
        if not any(has_devanagari(t) for t in tokens):
            return [strip_foreign(t) if has_foreign(t) else t for t in tokens]
        key = self._key(tokens)
        cached = self.memory.get(key)
        if cached is None and self._cache_path():
            cached = jsoncache.load_entries(self._cache_path(), VERSION).get(key)
        if (isinstance(cached, list) and len(cached) == len(tokens)
                and not any(has_devanagari(t) for t in cached)):
            self.stats["cached"] += 1
            self.memory[key] = cached
            return list(cached)
        self.stats["chunks"] += 1
        rough = [romanize_word(t) for t in tokens]
        result = rough
        if self._llm_ok():
            result = self._refine(tokens, rough)
        else:
            self.stats["rules"] += 1
        result = [romanize_word(t) if has_devanagari(t) else t for t in result]
        self.memory[key] = result
        if self._cache_path():
            jsoncache.merge(self._cache_path(), VERSION, {key: result})
        return result

    def _refine(self, tokens, rough):
        # Roman only: shown Devanagari, a small model copies it back.
        user = f"{len(rough)} words: " + json.dumps(rough, ensure_ascii=False)
        for _ in range(2):
            try:
                raw = self.provider.chat_json(SYSTEM_PROMPT, user, SCHEMA)
            except llm_mod.LLMError:
                continue
            words = raw.get("words") if isinstance(raw, dict) else None
            if not isinstance(words, list) or len(words) != len(tokens):
                continue          # misaligned: would break timestamps
            result = []
            for original, guess, base in zip(tokens, words, rough):
                if not has_devanagari(original):
                    result.append(original)        # English/Hinglish stays
                    continue
                cleaned = _clean_suggestion(guess, base)
                if cleaned is base and guess != base:
                    self.stats["rejected_words"] += 1
                result.append(cleaned)
            self.stats["llm"] += 1
            return result
        self.stats["rules"] += 1
        return rough

    def convert_words(self, words):
        """Copies of timed words with Roman text; timestamps untouched.

        Each converted word keeps its Whisper text in ``original``.
        """
        result = [dict(w) for w in words]
        chunk = []

        def flush():
            if not chunk:
                return
            converted = self.convert_tokens([result[i]["word"] for i in chunk])
            for i, text in zip(chunk, converted):
                if text != result[i]["word"]:
                    result[i]["original"] = result[i]["word"]
                    result[i]["word"] = text
                    if not re.search(r"\w", text):
                        result[i]["hidden"] = True     # nothing left to show
            chunk.clear()

        for index, word in enumerate(result):
            chunk.append(index)
            text = str(word.get("word", "")).strip()
            if (len(chunk) >= CHUNK_WORDS
                    or text.endswith((".", "!", "?", "।", "॥", "…"))):
                flush()
        flush()
        for word in result:
            assert_roman(word["word"], "Caption word")
        return result

    def text(self, text):
        """A short text (hook, title, summary) in Roman Hinglish."""
        text = str(text or "")
        if not has_devanagari(text):
            return strip_foreign(text)
        tokens = text.split()
        converted = []
        for start in range(0, len(tokens), CHUNK_WORDS):
            converted += self.convert_tokens(tokens[start:start + CHUNK_WORDS])
        return assert_roman(" ".join(converted), "Generated text")


# ------------------------------------------------- display transcript

def display_words(workdir, transcript, ranges=None, converter=None, log=None):
    """Words for captions/SRT under the Roman Hinglish policy.

    The original transcript is never modified. Words inside ``ranges``
    ([(start, end), ...]; None = all) get the full conversion (LLM spelling
    when available); every other word is transliterated by rules. The
    result is also written to transcript.display.json for inspection and
    export. Returns the full word list (same length and timing).
    """
    workdir = Path(workdir)
    words = transcript.get("words") or []
    if not any(has_devanagari(w.get("word")) or has_foreign(w.get("word")) for w in words):
        return [dict(w) for w in words]
    converter = converter or Converter(workdir, None, log)
    inside = [ranges is None or any(float(w["end"]) > a and float(w["start"]) < b
                                    for a, b in ranges) for w in words]
    result = []
    run = []

    def flush():
        if run:
            result.extend(converter.convert_words([words[i] for i in run]))
            run.clear()

    for index, word in enumerate(words):
        if inside[index]:
            run.append(index)
            continue
        flush()
        roman = romanize_word(word.get("word", ""))
        item = dict(word)
        if roman != item.get("word"):
            item["original"], item["word"] = item.get("word"), roman
            if not re.search(r"\w", roman):
                item["hidden"] = True
        result.append(item)
    flush()
    try:
        jsoncache.write_atomic(workdir / DISPLAY_FILE, {
            "policy": "hinglish-roman", "policy_version": POLICY_VERSION,
            "language": transcript.get("language"), "converter": converter.label,
            "words": result})
    except OSError:
        pass
    return result
