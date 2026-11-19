"""Phase 5: titles, descriptions, hashtags and tags for YouTube Shorts.

``generate`` asks the configured local LLM (Phase 2 provider, Ollama + Qwen by
default) for metadata in the clip's own language, then ``clean`` enforces
YouTube's limits. If the model is unavailable or answers badly, a rule-based
draft is used instead. Either way the result is only a draft: the user edits
and approves it before anything is uploaded.

YouTube limits enforced (YouTube Data API v3, videos resource):
  title 1-100 characters, description up to 5000 bytes, neither may contain
  "<" or ">"; tags up to 500 characters in total (a tag with a space counts
  two extra for quotes); more than 15 hashtags make YouTube ignore them all.
"""
import re

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
PROMPT_VERSION = 1

SYSTEM_PROMPT = (
    "You write YouTube Shorts metadata for ONE short clip, from its transcript.\n"
    "Write in the clip's own language and style: English stays English, Hindi "
    "in Devanagari stays Devanagari, and Hinglish (Hindi in Latin letters) "
    "stays Hinglish - do not translate it to English.\n"
    "title: under 70 characters about what happens in THIS clip; a specific "
    "hook, honest, no misleading clickbait, no hashtags. Never reuse the full "
    "video's title.\n"
    "description: 1-2 short sentences about the moment.\n"
    "hashtags: 3-5 meaningful topic words (names, game, theme) without '#', "
    "no spaces; never filler words or numbers.\n"
    "tags: 5-12 lowercase search phrases, words separated by spaces.\n"
    "Reply with JSON only."
)
HINDI_FILLER = set("""है हैं था थे थी का की के को ने से में पर और एक दो तीन यह वह ये वो
नाम बात कुछ भी तो ही हम तुम आप मैं""".split())
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
    word = re.sub(r"[^\wऀ-ॿ]", "", str(word or "").lstrip("#"))[:30]
    hindi = re.search(r"[ऀ-ॿ]", word)
    if (len(word) < 3 and not hindi) or word.isdigit() \
            or word.lower() in STOPWORDS or word in HINDI_FILLER:
        return ""
    return word


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
    for word in re.findall(r"[\wऀ-ॿ']+", str(text).lower()):
        word = word.strip("'")
        if len(word) < 4 or word in STOPWORDS or word.isdigit():
            continue
        counts[word] = counts.get(word, 0) + 1
    return [w for w, _ in sorted(counts.items(), key=lambda kv: (-kv[1], kv[0]))][:limit]


def fallback(info):
    """Rule-based draft: the hook as title, first sentence as description."""
    hook = _plain(info.get("hook")) or "Clip"
    text = _plain(info.get("text"))
    first = re.split(r"(?<=[.!?।])\s+", text)[0] if text else ""
    title = hook[:1].upper() + hook[1:]
    keywords = _keywords(text + " " + _plain(info.get("source_title")))
    return {"title": title, "description": first[:300], "hashtags": keywords[:3],
            "tags": keywords, "source": "rules"}


def _norm(text):
    return re.sub(r"[^\w\u0900-\u097F]+", " ", str(text or "").lower()).strip()


def _copies_video_title(title, source_title):
    title, source = _norm(title), _norm(source_title)
    return bool(title and source and (title == source or title in source))


def generate(info, provider=None, defaults=None):
    """Draft metadata for one clip. ``info``: hook, text, source_title, language,
    summary. Returns clean metadata plus "source": "ai" or "rules"."""
    draft = None
    reason = ""
    if provider is not None:
        ok, reason = provider.available()
        if ok:
            user = "\n".join(filter(None, [
                f"Video: {_plain(info.get('source_title'))[:150]}" if info.get("source_title") else "",
                f"Detected language: {info.get('language') or 'unknown'}",
                f"What happens: {_plain(info.get('summary'))[:200]}" if info.get("summary") else "",
                f"Clip transcript: {_plain(info.get('text'))[:1500]}"]))
            for _ in range(2):
                try:
                    raw = provider.chat_json(SYSTEM_PROMPT, user, SCHEMA)
                except llm_mod.LLMError as exc:
                    reason = str(exc)
                    continue
                if isinstance(raw, dict) and _plain(raw.get("title")):
                    draft = {**raw, "source": "ai"}
                    break
                reason = "The model returned no usable title."
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
    if source == "rules" and reason:
        result["note"] = f"AI unavailable ({reason}); simple draft used."
    elif source == "ai" and reason:
        result["note"] = reason
    return result


def provider_for(options):
    """The project's LLM provider, or None when AI is switched off."""
    if not options or not options.get("llm"):
        return None
    try:
        return llm_mod.get_provider(options.get("llm_provider") or "ollama", options["llm"])
    except ValueError:
        return None
