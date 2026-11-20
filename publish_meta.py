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
PROMPT_VERSION = 2   # 2: Roman-script Hinglish only (no Devanagari)
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
ghar paani maza mast toh bhi wala wali yeh woh""".split())
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
                or word.isdigit()):
            continue
        counts[word] = counts.get(word, 0) + 1
    return [w for w, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))][:limit]


def fallback(info):
    """Rule-based draft: the hook as title, first sentence as description."""
    hook = _plain(hinglish.romanize(info.get("hook"))) or "Clip"
    text = _plain(hinglish.romanize(info.get("text")))
    first = re.split(r"(?<=[.!?।])\s+", text)[0] if text else ""
    title = hook[:1].upper() + hook[1:]
    keywords = _keywords(text + " " + _plain(info.get("source_title")))
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
    text = str(text or "").strip()
    parts = re.split(r"(?<=[.!?])\s+", text)
    short = " ".join(parts[:sentences])
    if len(short) > limit:
        short = short[:limit].rsplit(" ", 1)[0].rstrip(",;:") + "…"
    return short


def looks_hinglish(text):
    words = re.findall(r"[a-z]+", str(text or "").lower())
    return any(w in HINGLISH_MARKERS for w in words)


def generate(info, provider=None, defaults=None):
    """Draft metadata for one clip. ``info``: hook, text, source_title, language,
    summary. Returns clean metadata plus "source": "ai" or "rules"."""
    draft = None
    reason = ""
    notes = []
    if provider is not None:
        ok, reason = provider.available()
        if ok:
            language = info.get("language") or "unknown"
            hindi = (language.startswith("hi") or hinglish.has_devanagari(info.get("text"))
                     or looks_hinglish(info.get("text")))
            if hinglish.has_devanagari(info.get("text")):
                language += " (shown here as Roman Hinglish)"
            # The model sees Roman text only, so it has no Devanagari to copy.
            user = "\n".join(filter(None, [
                f"Video: {_plain(hinglish.romanize(info.get('source_title')))[:150]}" if info.get("source_title") else "",
                f"Detected language: {language}",
                (f"What happens (English context only - do not copy its language): "
                 f"{_plain(hinglish.romanize(info.get('summary')))[:200]}"
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
                if devanagari_fields(raw):
                    # Never accepted silently: ask again, then transliterate.
                    reason = "The model wrote Devanagari."
                    prompt = user + "\n" + RETRY_NOTE
                    continue
                if hindi and not asked_hinglish and not looks_hinglish(
                        f"{raw.get('title')} {raw.get('description')}"):
                    asked_hinglish = True        # one more try for Hinglish
                    prompt = user + "\n" + HINGLISH_NOTE
                    reason = ""
                    continue
                reason = ""
                break
            if (draft is not None and hindi and not devanagari_fields(draft)
                    and not looks_hinglish(f"{draft.get('title')} {draft.get('description')}")):
                notes.append("The AI wrote this Hindi clip's title in English; "
                             "edit it into Hinglish if you prefer.")
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
        reason = "AI title repeated the video title; used the clip's hook."
    source = draft.pop("source")
    try:
        result = clean(draft, defaults)
    except ValueError:
        result = clean(fallback(info), defaults)
        source = "rules"
    result["source"] = source
    result["policy"] = "hinglish-roman"
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
