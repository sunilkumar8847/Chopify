"""Phase 5: titles, descriptions, hashtags and tags for YouTube Shorts.

``generate`` asks the configured local LLM (Phase 2 provider, Ollama + Qwen by
default) for metadata, then ``clean`` enforces YouTube's limits and the
output-language policy: English stays English, Hindi and Hinglish are written
as Roman-script Hinglish, and Devanagari is never accepted (see hinglish.py). If the model is unavailable or answers badly, a rule-based
draft is used instead. Either way the result is only a draft: the user edits
and approves it before anything is uploaded.

YouTube limits enforced (YouTube Data API v3, videos resource):
  title 1-100 characters, description up to 5000 bytes, neither may contain
  "<" or ">"; tags up to 500 characters in total (a tag with a space counts
  two extra for quotes); more than 15 hashtags make YouTube ignore them all.
"""
import re

import hinglish
import llm as llm_mod

TITLE_MAX = 100
DESCRIPTION_MAX_BYTES = 5000
TAGS_MAX_CHARS = 500
HASHTAGS_MAX = 5              # well under YouTube's 15
PRIVACY = ("private", "unlisted", "public")
DEFAULT_PRIVACY = "private"
CATEGORIES = {"1": "Film & Animation", "2": "Autos & Vehicles", "10": "Music",
              "15": "Pets & Animals", "17": "Sports", "19": "Travel & Events",
              "20": "Gaming", "22": "People & Blogs", "23": "Comedy",
              "24": "Entertainment", "25": "News & Politics",
              "26": "Howto & Style", "27": "Education", "28": "Science & Technology"}
PROMPT_VERSION = 3   # 3: Hinglish checked per field; speaker-line fallback
POLICY_FIELDS = ("title", "description", "hashtags", "tags")

SYSTEM_PROMPT = (
    "You write YouTube Shorts metadata for ONE short clip, from its transcript.\n"
    "LANGUAGE (strict): write only in Latin/Roman letters. English clips stay "
    "natural English. Hindi or Hinglish clips are written in natural "
    "Hinglish: Hindi words typed in Roman letters the way Indians type on "
    "WhatsApp (\"Bhai aaj ka gameplay bahut crazy tha\"), with English words "
    "kept in English. Never use Devanagari script. Do not translate Hinglish "
    "into formal English; keep the slang, humour and tone.\n"
    "title: under 70 characters about what happens in THIS clip; a specific "
    "hook, honest, no misleading clickbait, no hashtags. Never reuse the full "
    "video's title.\n"
    "description: 1-2 short sentences about the moment.\n"
    "hashtags: 3-5 meaningful topic words (names, game, theme) without '#', "
    "no spaces; never filler words or numbers.\n"
    "tags: 5-12 lowercase search phrases, words separated by spaces.\n"
    "Reply with JSON only."
)
HINDI_NOTE = (
    "This clip is spoken in Hindi; its transcript above is already written in "
    "Roman letters. Write the title and description in the same Roman-letter "
    "Hinglish as the transcript: reuse the speaker's own words and phrases, keep "
    "English words in English. Use only Latin letters (a-z).")
HINGLISH_NOTE = ("Your title was plain English. The clip is Hindi: rewrite the "
                 "title and description in natural Hinglish (Roman letters).")
HINGLISH_MARKERS = set("""hai hain tha thi the kya nahi nahin bhai yaar aaj kal
mein main maine mujhe hum tum aap ka ki ke ko se ne bahut accha acha kaise kyun
kyunki raha rahi rahe gaya gayi hua kar karo karna dekho chalo pehli pehla baar
ghar paani maza mast toh bhi wala wali yeh woh aapke aapka aapki unka unki unke
inka inki inke apna apni apne kuch koi abhi phir dekh dekha lag lagta lagega
chal chala gaye aaya aayi""".split())
RETRY_NOTE = ("Your previous answer contained Devanagari letters. Rewrite every "
              "field in Roman (Latin) letters only, as natural Hinglish.")
HINDI_FILLER = set("""hai hain tha the thi ka ki ke ko ne se mein me par pe aur ek do
teen yeh ye woh wo naam baat kuch bhi toh to hi hum tum aap main nahi kya
koi sab abhi phir bas haan ji arre yaar raha rahi rahe gaya kar karo""".split())
SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "description": {"type": "string"},
        "hashtags": {"type": "array", "items": {"type": "string"}},
        "tags": {"type": "array", "items": {"type": "string"}},
    },
    "required": ["title", "description", "hashtags", "tags"],
}
STOPWORDS = set("""a an the and or but so to of in on at for with from by is are was
were be been it this that these those i you he she we they me my your our his her
their them us just like really very not no yes do does did have has had will would
can could should what when where who why how about into out up down over than then
there here oh okay ok yeah gonna wanna got get""".split())


def _plain(text):
    text = re.sub(r"[<>]", "", str(text or ""))
    return re.sub(r"\s+", " ", text).strip()


def _hashtag(word):
    word = re.sub(r"[^\w]", "", str(word or "").lstrip("#"))[:30]
    if (len(word) < 3 or word.isdigit() or word.lower() in STOPWORDS
            or word.lower() in HINDI_FILLER or hinglish.has_devanagari(word)):
        return ""
    return word


def devanagari_fields(meta):
    """Names of policy fields that contain Devanagari."""
    found = []
    for field in POLICY_FIELDS:
        value = meta.get(field) if isinstance(meta, dict) else None
        values = value if isinstance(value, list) else [value]
        if any(hinglish.has_devanagari(v) for v in values):
            found.append(field)
    return found


def romanize_meta(meta):
    """Transliterate the policy fields (safe fallback for model output)."""
    result = dict(meta)
    for field in POLICY_FIELDS:
        value = result.get(field)
        if isinstance(value, list):
            result[field] = [hinglish.romanize(v) for v in value]
        elif value is not None:
            result[field] = hinglish.romanize(value)
    return result


def _truncate_bytes(text, limit):
    data = text.encode("utf-8")
    if len(data) <= limit:
        return text
    return data[:limit].decode("utf-8", errors="ignore").rstrip()


def clean(meta, defaults=None):
    """Return metadata YouTube will accept (raises ValueError if unusable)."""
    defaults = defaults or {}
    if not isinstance(meta, dict):
        raise ValueError("Metadata must be an object.")
    bad = devanagari_fields(meta)
    if bad:
        suggestion = romanize_meta(meta).get("title") if "title" in bad else ""
        raise ValueError(
            "Output policy: write " + ", ".join(bad) + " in Roman letters "
            "(Hinglish), not Devanagari."
            + (f' Suggested title: "{_plain(suggestion)}"' if suggestion else ""))
    title = _plain(meta.get("title"))
    if len(title) > TITLE_MAX:
        title = title[:TITLE_MAX - 1].rstrip() + "…"
    if not title:
        raise ValueError("A title is required.")
    hashtags = []
    for tag in meta.get("hashtags") or []:
        tag = _hashtag(tag)
        if tag and tag.lower() not in {h.lower() for h in hashtags}:
            hashtags.append(tag)
    if "shorts" not in {h.lower() for h in hashtags}:
        hashtags.insert(0, "Shorts")
    hashtags = hashtags[:HASHTAGS_MAX + 1]
    description = str(meta.get("description") or "").replace("<", "").replace(">", "")
    description = re.sub(r"[ \t]+", " ", description).strip()
    tags, total = [], 0
    for tag in meta.get("tags") or []:
        tag = _plain(tag).replace(",", " ").strip()[:100]
        if not tag or tag.lower() in {t.lower() for t in tags}:
            continue
        cost = len(tag) + (2 if " " in tag else 0) + (1 if tags else 0)
        if total + cost > TAGS_MAX_CHARS:
            break
        tags.append(tag)
        total += cost
    privacy = meta.get("privacy", defaults.get("privacy", DEFAULT_PRIVACY))
    if privacy not in PRIVACY:
        raise ValueError("Privacy must be private, unlisted or public.")
    category = str(meta.get("category_id", defaults.get("category_id", "22")))
    if category not in CATEGORIES:
        raise ValueError("Unknown YouTube category.")
    kids = meta.get("made_for_kids", defaults.get("made_for_kids", False))
    if not isinstance(kids, bool):
        raise ValueError("made_for_kids must be true or false.")
    tag_line = " ".join("#" + h for h in hashtags)
    body = description
    if tag_line and tag_line not in body:
        body = (body + "\n\n" + tag_line).strip()
    body = _truncate_bytes(body, DESCRIPTION_MAX_BYTES)
    return {"title": title, "description": body, "hashtags": hashtags,
            "tags": tags, "privacy": privacy, "category_id": category,
            "made_for_kids": kids}


def _keywords(text, limit=8):
    counts = {}
    for word in re.findall(r"[\w']+", hinglish.romanize(text).lower()):
        word = word.strip("'")
        if (len(word) < 4 or word in STOPWORDS or word in HINDI_FILLER
                or word in HINGLISH_WORDS or word.isdigit()):
            continue           # grammar and everyday words are not topics
        counts[word] = counts.get(word, 0) + 1
    return [w for w, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))][:limit]


POLICY = "hinglish-roman-2"
# Words that only Hindi/Hinglish uses (English look-alikes such as "main",
# "the", "to", "do", "me", "hi" are left out so English is not mistaken).
_AMBIGUOUS = {"main", "the", "to", "do", "me", "hi", "ji", "bas", "set", "pe",
              "par", "use", "kal", "do", "na", "ho", "us", "jo", "ab", "tab",
              "sab", "ek", "ki", "ka", "ke", "ko", "se", "ne", "ye"}
EVERYDAY_HINGLISH = set("""tarah paani dikh dikhta dikha lagta laga lagi lage lagega
karna karte karti kiya kiye karenge karega karungi karunga hoga hogi honge jaana
jaata jaati aana aata aati bola bolo bolta suno sun ruk ruko jao aao mila mili mile
samajh samjha socha sochta chahiye sakta sakti sakte khud sirf jaldi dheere bahar
andar peeche aage baad pehle sabse kaam cheez cheezein galti sach jhooth kitna
kitni kaun kaise kab jab yahan wahan kahan kyun isliye magar lekin behen pagal
mazaa paisa paise poora poori pura puri vasool bada badi bade chhota chhoti naya
nayi purana dekho dekhte dekhna dekhenge chalo chalte chala chali gaadi ghoom
maar maara bach bacha bhaag bhaago pakad pakdo uda udaa gira giri gire""".split())
HINGLISH_WORDS = (HINGLISH_MARKERS | EVERYDAY_HINGLISH
                  | {w for value in hinglish.COMMON.values() for w in value.split()}) - _AMBIGUOUS

# Short Hindi function words count only next to other Hindi words.
_WEAK_HINDI = {"ka", "ki", "ke", "ko", "se", "ne", "ye", "pe", "par", "ek", "ho",
               "na", "jo", "ab", "sab", "kal", "us", "bas", "toh", "bhi", "hi"}
_PROMPT_ECHO = re.compile(
    r"what happens|1-2 sentences|short hook|in hinglish|clip transcript|"
    r"detected language|english context|roman letters|latin letters", re.I)
# English grammar words. Hinglish keeps English nouns ("car", "queue") but
# uses Hindi grammar, so these decide whether a sentence is English.
# Words Hindi also uses ("to", "do", "me", "hi", "us", "so", "main") are left out.
ENGLISH_FUNCTION = set("""the a an is are was were be been being and but or with
this that these those their there they he she it its his her our your my we you
of for from into at on by about while because which who whom what when where why
how than then just very can could will would should has have had does did not
tries trying gets finds realizes realises actually own someone person people""".split())


# Words a reader recognises (Hinglish, English, Devanagari-spelled English).
KNOWN_WORDS = (HINGLISH_WORDS | _WEAK_HINDI | ENGLISH_FUNCTION | STOPWORDS
               | {w for value in hinglish.ENGLISH.values() for w in value.lower().split()})
PROMPT_WORDS = {"detected", "language", "detectedlanguage", "roman", "hinglish",
                "transcript", "clip", "cliptranscript", "letters", "latin", "devanagari",
                "context", "instructions"}


def hinglish_score(text):
    """(hindi words, all words) - weak function words count half."""
    words = re.findall(r"[a-z]+", hinglish.romanize(text).lower())
    strong = sum(w in HINGLISH_WORDS for w in words)
    weak = sum(w in _WEAK_HINDI for w in words)
    return strong + (0.5 * weak if strong else 0), len(words)


def is_hinglish(text, short=False):
    """True when a text is really Hinglish, not English with a stray word."""
    hindi, total = hinglish_score(text)
    if not total:
        return False
    need = 1 if short or total <= 5 else 2
    return hindi >= need and hindi / total >= 0.2


def is_english(text):
    """True when English grammar dominates (Hindi words, if any, are isolated)."""
    words = re.findall(r"[a-z']+", hinglish.romanize(text).lower())
    hindi, _ = hinglish_score(text)
    english = sum(w in ENGLISH_FUNCTION for w in words)
    return (hindi == 0 and english >= 1) or english > hindi + 1


def clip_style(info):
    """'hinglish' for Hindi or Hinglish speech, else 'english'."""
    text = info.get("text") or ""
    if hinglish.has_devanagari(text) or str(info.get("language") or "").startswith("hi"):
        return "hinglish"
    return "hinglish" if is_hinglish(text) else "english"


def _sentences(text):
    return [part.strip() for part in re.split(r"(?<=[.!?])\s+", str(text or ""))
            if part.strip()]


def speaker_lines(info, count=2):
    """The speaker's own best lines (Roman script), in spoken order.

    Used when the AI cannot write natural Hinglish: the words are the
    creator's own, so the tone is right by construction.
    """
    roman = _plain(hinglish.romanize(info.get("text")))
    scored = []
    for index, line in enumerate(_sentences(roman)):
        line = re.sub(r"\b(\w+)(?:[ ,!]+\1\b)+", r"\1", line, flags=re.I)   # "vaai! vaai!"
        line = re.sub(r"([!?.])[!?.]+", r"\1", line).strip(" ,")
        words = re.findall(r"[A-Za-z0-9']+", line)
        if not 3 <= len(words) <= 20:
            continue
        hindi, _ = hinglish_score(line)
        odd = sum(len(w) == 1 and w.lower() not in ("a", "i") for w in words)
        known = sum(w.lower() in KNOWN_WORDS or w[:1].isupper() or w.isdigit()
                    for w in words) / len(words)
        if hindi < 1 or known < 0.6 or _repetitive(line):
            continue
        # Mostly recognisable words first (Whisper's mishearings sound odd),
        # then Hindi-rich, then a natural length.
        score = 8 * known + hindi + min(len(words), 12) / 4 - 2 * odd
        scored.append((score, index, line))
    best = sorted(scored, reverse=True)[:count]
    return [line[:1].upper() + line[1:] for _, _, line in sorted(best, key=lambda t: t[1])]


def _speaker_title(info):
    lines = speaker_lines(info, count=1)
    if not lines:
        hook = _plain(hinglish.romanize(info.get("hook")))
        return hook[:1].upper() + hook[1:] if hook else ""
    line = lines[0]
    words = line.split()
    if len(words) <= 10:
        return line
    head = " ".join(words[:10])
    if "," in head:                       # end at a natural pause
        return head[:head.rindex(",")]
    return head.rstrip(",;:") + "…"


def _contains_title_part(text, source_title):
    """True if a 3+ word part of the video title appears verbatim."""
    body = f" {_norm(text)} "
    for part in re.split(r"[|:\-–—]", str(source_title or "")):
        part = _norm(part)
        if len(part.split()) >= 3 and f" {part} " in body:
            return True
    return False


def _first_line(info):
    """The clip's first spoken sentence in Roman script (last resort)."""
    for line in _sentences(_plain(hinglish.romanize(info.get("text")))):
        words = line.split()
        if len(words) >= 3:
            line = " ".join(words[:25])
            return line[:1].upper() + line[1:]
    return ""


def fallback(info):
    """Rule-based draft. Hindi/Hinglish clips use the speaker's own lines."""
    hook = _plain(hinglish.romanize(info.get("hook"))) or "Clip"
    text = _plain(hinglish.romanize(info.get("text")))
    keywords = _keywords(text + " " + _plain(info.get("source_title")))
    if clip_style(info) == "hinglish":
        lines = speaker_lines(info)
        title = _speaker_title(info) or hook[:1].upper() + hook[1:]
        description = " ".join(lines) or (_sentences(text) or [""])[0][:300]
        return {"title": title, "description": description, "hashtags": keywords[:3],
                "tags": keywords, "source": "rules"}
    first = _sentences(text)[0] if text else ""
    title = hook[:1].upper() + hook[1:]
    return {"title": title, "description": first[:300], "hashtags": keywords[:3],
            "tags": keywords, "source": "rules"}


def _norm(text):
    return re.sub(r"[^\w]+", " ", hinglish.romanize(text).lower()).strip()


def _copies_video_title(title, source_title):
    title, source = _norm(title), _norm(source_title)
    if not (title and source):
        return False
    if title == source or title in source:
        return True
    words = [w for w in title.split() if len(w) > 1]
    # the same words in another order ("GTA 6 MY FIRST DAY IN VICE CITY")
    return len(words) >= 3 and sum(w in source.split() for w in words) >= 0.8 * len(words)


def _short_description(text, sentences=2, limit=300):
    """An AI description is 1-2 sentences; a small model sometimes pastes
    the whole transcript instead."""
    short = " ".join(_sentences(text)[:sentences])
    if len(short) > limit:
        short = short[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return short


def looks_hinglish(text):
    return is_hinglish(text, short=True)


def _copies_summary(text, summary):
    words = set(_norm(text).split())
    source = [w for w in _norm(summary).split() if len(w) > 2]
    return len(source) >= 4 and sum(w in words for w in source) >= 0.7 * len(source)


def field_problem(field, value, info):
    """Why a Hindi/Hinglish clip's AI title/description is unusable, or None."""
    value = str(value or "").strip()
    if not value:
        return "empty"
    if hinglish.has_devanagari(value):
        return "Devanagari"
    if _PROMPT_ECHO.search(value):
        return "copied the instructions"
    if field == "description" and _copies_summary(value, info.get("summary")):
        return "English"
    if field == "description" and (_copies_summary(value, info.get("source_title"))
                                   or _contains_title_part(value, info.get("source_title"))):
        return "copied the video title"
    if is_english(value):
        return "English"
    if _repetitive(value):
        return "repetitive"
    hindi, total = hinglish_score(value)
    if field == "description" and total >= 5 and hindi < 1.5:
        return "not Hinglish"
    return None


def _repetitive(text):
    words = re.findall(r"[a-z']+", str(text or "").lower())
    grams = [" ".join(words[i:i + 2]) for i in range(len(words) - 1)]
    return any(grams.count(g) >= 3 for g in set(grams))


def _hinglish_retry_note(problems):
    fields = " and ".join(sorted(problems))
    return (f"Your {fields} was not natural Hinglish. Rewrite the {fields} as the "
            "speaker would say it: Hindi words in Roman letters (for example "
            "hai, raha, kya, mein, yaar, bahut) mixed with the English words he "
            "uses. Not English, not Devanagari, and do not repeat these "
            "instructions.")


def generate(info, provider=None, defaults=None):
    """Draft metadata for one clip. ``info``: hook, text, source_title, language,
    summary. Returns clean metadata plus "source": "ai" or "rules".

    Language follows the speech: English stays English; Hindi and Hinglish
    get natural Roman-script Hinglish. An English or Devanagari answer for a
    Hindi/Hinglish clip is never accepted silently: the model is asked again
    with a specific instruction, and a field that is still wrong is replaced
    by the speaker's own lines (with a note saying so)."""
    draft = None
    reason = ""
    notes = []
    style = clip_style(info)
    hindi = style == "hinglish"
    if provider is not None:
        ok, reason = provider.available()
        if ok:
            language = info.get("language") or "unknown"
            if hinglish.has_devanagari(info.get("text")):
                language += " (shown here as Roman Hinglish)"
            # The model sees Roman text only, so it has no Devanagari to copy.
            user = "\n".join(filter(None, [
                f"Video: {_plain(hinglish.romanize(info.get('source_title')))[:150]}" if info.get("source_title") else "",
                f"Detected language: {language}",
                (f"Context: {_plain(hinglish.romanize(info.get('summary')))[:200]}"
                 if info.get("summary") else ""),
                f"Clip transcript: {_plain(hinglish.romanize(info.get('text')))[:1500]}",
                HINDI_NOTE if hindi else ""]))
            prompt = user
            asked_hinglish = False
            for _ in range(3):
                try:
                    raw = provider.chat_json(SYSTEM_PROMPT, prompt, SCHEMA)
                except llm_mod.LLMError as exc:
                    reason = str(exc)
                    continue
                if not (isinstance(raw, dict) and _plain(raw.get("title"))):
                    reason = "The model returned no usable title."
                    continue
                draft = {**raw, "source": "ai"}
                reason = ""
                if hindi:
                    problems = {f for f in ("title", "description")
                                if field_problem(f, raw.get(f), info)}
                    if problems and not asked_hinglish:
                        asked_hinglish = True     # one specific retry
                        prompt = user + "\n" + _hinglish_retry_note(problems)
                        continue
                    break
                if devanagari_fields(raw):
                    # Never accepted silently: ask again, then transliterate.
                    reason = "The model wrote Devanagari."
                    prompt = user + "\n" + RETRY_NOTE
                    continue
                break
    if draft is not None and hindi:
        replaced = []
        for field in ("title", "description"):
            problem = field_problem(field, draft.get(field), info)
            if problem is None:
                continue
            substitute = (_speaker_title(info) if field == "title"
                          else " ".join(speaker_lines(info)) or _first_line(info))
            if not substitute:
                hook = _plain(hinglish.romanize(info.get("hook")))
                substitute = hook[:1].upper() + hook[1:]
            if substitute:
                draft[field] = substitute
                replaced.append(f"{field} ({problem})")
            else:
                draft[field] = hinglish.romanize(draft.get(field) or "")
                replaced.append(f"{field} ({problem}; nothing better was available)")
        if replaced:
            notes.append("The AI did not write natural Hinglish for the "
                         + ", ".join(replaced)
                         + "; Chopify used the speaker's own words instead.")
    if draft is not None:
        keywords = _keywords(_plain(hinglish.romanize(info.get("text"))) + " "
                             + _plain(info.get("source_title")))
        for field in ("tags", "hashtags"):
            values = draft.get(field) if isinstance(draft.get(field), list) else []
            if any(hinglish.has_devanagari(v) for v in values):
                values = keywords if field == "tags" else keywords[:3]
            draft[field] = [v for v in values if not (
                set(re.findall(r"[a-z]+", str(v).lower())) & PROMPT_WORDS)]
    if draft is not None and devanagari_fields(draft):
        draft = romanize_meta(draft)
        notes.append("The AI wrote Devanagari; it was transliterated to "
                     "Roman Hinglish. Please review the wording.")
        reason = ""
    if draft is not None:
        # other scripts a small model sometimes mixes in (policy: Roman only)
        for field in ("title", "description"):
            if isinstance(draft.get(field), str):
                draft[field] = hinglish.strip_foreign(draft[field])
        draft["description"] = _short_description(draft.get("description"))
    if draft is None:
        draft = fallback(info)
    elif _copies_video_title(draft.get("title"), info.get("source_title")):
        # A small model sometimes just repeats the full video's title.
        draft["title"] = fallback(info)["title"]
        notes.append("AI title repeated the video title; used the clip's own words.")
    source = draft.pop("source")
    try:
        result = clean(draft, defaults)
    except ValueError:
        result = clean(fallback(info), defaults)
        source = "rules"
    result["source"] = source
    result["policy"] = POLICY
    result["language_style"] = style
    if source == "rules" and reason:
        notes.insert(0, f"AI unavailable ({reason}); simple draft used.")
    elif source == "ai" and reason:
        notes.insert(0, reason)
    if notes:
        result["note"] = " ".join(notes)
    return result


def provider_for(options):
    """The project's LLM provider, or None when AI is switched off."""
    if not options or not options.get("llm"):
        return None
    try:
        return llm_mod.get_provider(options.get("llm_provider") or "ollama", options["llm"])
    except ValueError:
        return None
