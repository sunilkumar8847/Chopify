"""Output-language policy: Roman-script Hinglish, never Devanagari."""
import json

import pytest

import captions
import hinglish
import llm
import projects

DEVANAGARI = hinglish.has_devanagari


# ------------------------------------------------------ conversion rules

@pytest.mark.parametrize("source, expected", [
    # the user's three reference examples
    ("आज मैंने पहली बार GTA 6 खेला और बहुत मज़ा आया।",
     "aaj maine pehli baar GTA 6 khela aur bahut maza aaya."),
    ("Bhai aaj ka gameplay bahut crazy tha, mujhe expect nahi tha!",
     "Bhai aaj ka gameplay bahut crazy tha, mujhe expect nahi tha!"),
    ("This is the craziest mission I've played.",
     "This is the craziest mission I've played."),
    # mixed: English words kept, Hindi romanized, Devanagari-spelled English restored
    ("भाई ये गेम में नहीं फिसिक्स बहुत क्रेजी हैं!",
     "bhai ye game mein nahi physics bahut crazy hain!"),
    ("bro ये mission बहुत tough था", "bro ye mission bahut tough tha"),
    ("स्वागत आप सभी का फर्स्ट गेमप्ले वीडियो में",
     "swagat aap sabhi ka first gameplay video mein"),
])
def test_reference_sentences(source, expected):
    assert hinglish.romanize(source) == expected


@pytest.mark.parametrize("word, expected", [
    ("करते", "karte"), ("समझ", "samajh"), ("लड़का", "ladka"), ("बदलना", "badalna"),
    ("संभव", "sambhav"), ("हिंदी", "hindi"), ("मित्र", "mitra"), ("कमल", "kamal"),
    ("जीत", "jeet"), ("सीधा", "seedha"), ("दूर", "door"), ("रहा", "raha"),
    ("गिरेगा", "girega"), ("बंदा", "banda"), ("क़िला", "qila"), ("फ़ोटो", "foto"),
    ("दुःख", "dukh"), ("२०२६", "2026"), ("हाँ", "haan"),
])
def test_transliteration_rules(word, expected):
    assert hinglish.romanize_word(word) == expected


def test_punctuation_and_danda_survive():
    assert hinglish.romanize("क्या?! (भाई)") == "kya?! (bhai)"
    assert hinglish.romanize("खत्म। अब") == "khatam. ab"


def test_nothing_unknown_ever_leaves_as_devanagari():
    weird = "क़ऺ़ॐ ऽ ॰ ꣸ अ॑ ऀ"
    assert not DEVANAGARI(hinglish.romanize(weird))


def test_assert_roman_rejects_devanagari():
    with pytest.raises(ValueError):
        hinglish.assert_roman("ok भाई")
    assert hinglish.assert_roman("ok bhai") == "ok bhai"


# ---------------------------------------------- timed words (captions)

def timed(text, start=10.0, step=0.5):
    return [{"word": w, "start": start + i * step, "end": start + i * step + 0.4}
            for i, w in enumerate(text.split())]


def test_converted_words_keep_exact_timestamps_and_originals():
    words = timed("आज मैंने पहली बार GTA 6 खेला।")
    out = hinglish.Converter().convert_words(words)
    assert [(w["start"], w["end"]) for w in out] == [(w["start"], w["end"]) for w in words]
    assert [w["word"] for w in out] == ["aaj", "maine", "pehli", "baar", "GTA", "6", "khela."]
    assert out[0]["original"] == "आज" and "original" not in out[4]
    assert words[0]["word"] == "आज", "input is not modified"


def test_english_transcripts_are_untouched():
    words = timed("This is the craziest mission I've played.")
    assert hinglish.Converter().convert_words(words) == words


def test_srt_uses_the_converted_text_with_the_same_timing(tmp_path):
    words = timed("भाई ये गेम में फिसिक्स बहुत क्रेजी हैं!", start=0.0)
    out = hinglish.Converter().convert_words(words)
    captions.write_srt(out, 0.0, 10.0, tmp_path / "a.srt", group_size=4)
    captions.write_srt(words, 0.0, 10.0, tmp_path / "b.srt", group_size=4)
    roman, original = (tmp_path / "a.srt").read_text("utf-8"), (tmp_path / "b.srt").read_text("utf-8")
    assert not DEVANAGARI(roman) and "bhai ye game mein" in roman
    timing = lambda s: [line for line in s.splitlines() if "-->" in line]
    assert timing(roman) == timing(original), "same cue timing as the original"


def test_longer_roman_words_still_wrap_into_short_cues():
    words = timed("ज़िन्दगी इंतज़ार धन्यवाद शुक्रिया इंपॉर्टेंट एक्चुअली बेसिकली", start=0.0)
    out = hinglish.Converter().convert_words(words)
    for group in captions.group_words(out, 8, max_chars=18):
        assert len(" ".join(w["word"] for w in group)) <= 18 or len(group) == 1


# ------------------------------------------------- optional LLM spelling

class SpellLLM(llm.Provider):
    name = "fake"

    def __init__(self, fix=None, ok=True):
        super().__init__("speller")
        self.fix, self.ok, self.calls = fix or {}, ok, []

    def available(self):
        return (self.ok, "" if self.ok else "off")

    def chat_json(self, system, user, schema=None):
        self.calls.append(user)
        words = json.loads(user.split(": ", 1)[1])
        return {"words": [self.fix.get(w, w) for w in words]}


def test_llm_spelling_is_accepted_only_when_it_is_the_same_word():
    tokens = ["सवागता", "आप", "हमें", "वीडियो"]
    speller = SpellLLM({"savaagta": "swagat", "aap": "you",      # translation
                        "humein": "हमें", "video": "video"})      # Devanagari echo
    out = hinglish.Converter(None, speller).convert_tokens(tokens)
    assert out == ["swagat", "aap", "humein", "video"]
    assert not any(DEVANAGARI(t) for t in out)
    assert not any("ऀ" <= ch <= "ॿ" for ch in speller.calls[0]), \
        "the model is never shown Devanagari to copy"


def test_misaligned_llm_answers_fall_back_to_rules():
    class Dropper(SpellLLM):
        def chat_json(self, system, user, schema=None):
            self.calls.append(user)
            return {"words": ["only", "two"]}
    tokens = ["आज", "मैंने", "पहली", "बार"]
    speller = Dropper()
    out = hinglish.Converter(None, speller).convert_tokens(tokens)
    assert out == ["aaj", "maine", "pehli", "baar"] and len(speller.calls) == 2


def test_unavailable_llm_means_rules_only():
    speller = SpellLLM(ok=False)
    assert hinglish.Converter(None, speller).convert_tokens(["भाई"]) == ["bhai"]
    assert speller.calls == []


def test_conversions_are_cached_per_project(tmp_path):
    speller = SpellLLM({"savaagta": "swagat"})
    first = hinglish.Converter(tmp_path, speller).convert_tokens(["सवागता", "भाई"])
    again = SpellLLM()
    second = hinglish.Converter(tmp_path, again).convert_tokens(["सवागता", "भाई"])
    assert first == second == ["swagat", "bhai"] and again.calls == []
    assert (tmp_path / hinglish.CACHE_FILE).is_file()


def test_sounds_alike():
    assert hinglish.sounds_alike("gemple", "gameplay")
    assert hinglish.sounds_alike("vidiyo", "video")
    assert hinglish.sounds_alike("phorst", "first")
    assert not hinglish.sounds_alike("humein", "we")
    assert not hinglish.sounds_alike("aane", "coming")


# ------------------------------------------- display transcript, projects

def test_display_transcript_is_separate_and_complete(tmp_path):
    transcript = {"language": "hi", "words": timed("आज मैंने पहली बार GTA 6 खेला", 0.0)
                  + timed("और बहुत मज़ा आया।", 100.0)}
    before = json.dumps(transcript, ensure_ascii=False)
    words = hinglish.display_words(tmp_path, transcript, ranges=[(99.0, 110.0)])
    assert json.dumps(transcript, ensure_ascii=False) == before, "original kept"
    assert len(words) == len(transcript["words"])
    assert " ".join(w["word"] for w in words) == \
        "aaj maine pehli baar GTA 6 khela aur bahut maza aaya."
    saved = json.loads((tmp_path / hinglish.DISPLAY_FILE).read_text("utf-8"))
    assert saved["policy"] == "hinglish-roman" and len(saved["words"]) == len(words)


def test_existing_project_text_is_upgraded_without_reprocessing(tmp_path):
    segments = [{"start": 1.0, "end": 9.0, "hook": "जाएद बीच साइट पे है क्या", "overall": 8.5,
                 "judge": {"summary": "A queue joke.", "reason": "मज़ेदार"}}]
    (tmp_path / "segments.json").write_text(json.dumps(segments, ensure_ascii=False), "utf-8")
    assert projects.apply_text_policy(tmp_path, {}) == 2
    out = json.loads((tmp_path / "segments.json").read_text("utf-8"))[0]
    assert out["hook"] == "jaayed beech saait pe hai kya"
    assert out["hook_original"] == "जाएद बीच साइट पे है क्या"
    assert out["judge"]["summary"] == "A queue joke." and not DEVANAGARI(out["judge"]["reason"])
    assert projects.apply_text_policy(tmp_path, {}) == 0, "idempotent"


def test_hinglish_ai_option_is_validated():
    assert projects.validate_options({})["hinglish_ai"] is False
    assert projects.validate_options({"hinglish_ai": True})["hinglish_ai"] is True
    with pytest.raises(ValueError):
        projects.validate_options({"hinglish_ai": "yes"})


def test_other_scripts_hallucinated_by_whisper_are_removed():
    assert hinglish.strip_foreign("aausuさ .ными.") == "aausu .."
    assert hinglish.strip_foreign("café naïve ok 😂 ٣") == "café naïve ok 😂 3"
    words = timed("रहा हैं आउसूさ .ными. ありがとう")
    out = hinglish.Converter().convert_words(words)
    assert [w["word"] for w in out] == ["raha", "hain", "aausu", "..", ""]
    assert [bool(w.get("hidden")) for w in out] == [False, False, False, True, True]
    assert out[-1]["hidden"] is True and out[-1]["original"] == "ありがとう"
    assert [(w["start"], w["end"]) for w in out] == [(w["start"], w["end"]) for w in words]
