"""Which language did the user write in?

WHAT WORKS AND WHAT DOES NOT. Detection works and the final answer comes back
in the language of the question. The other half of the idea -- forcing the
internal debate into English -- DOES NOT WORK, and the instruction is kept only
because it is cheap and harmless.

Tested twice against gpt-oss-120b with an Uzbek question: once with the
instruction at the end of the system prompt, once at the front in capitals and
declared non-negotiable. Both times the model answered in Uzbek. A model
mirrors the language of the question, and no amount of system-prompt emphasis
overrides that reliably.

Nothing depends on it. The seat still reasons well in Uzbek, the user-facing
seats still reply in the user's language, and the only loss is cross-vendor
consistency in the middle of the pipeline. Enforcing it properly would need a
translation pass per seat -- more calls, more latency, more to go wrong -- to
fix something that is not costing anything.

The original reasoning, kept for the record:

  * Consistency. Five models from four vendors arguing across three languages
    compounds every weakness each of them has. English is the language all of
    them handle best, so the REASONING happens there.
  * The reader. Nobody wants a Russian question answered in English because
    the machinery preferred it.

Only the user-facing seats -- the Synthesis and the Judge -- are told to switch.
The proposers, the Drafter and the Critic are talking to each other, and a
translation layer in the middle of a debate would lose precision for no reader.

WHY NOT A LIBRARY. langdetect and friends are a dependency and a model download
to answer a question that, for this purpose, is nearly trivial: the alphabet
answers it most of the time, and a short word list settles Uzbek versus Turkish.
Being wrong is cheap -- the answer arrives in English, which everyone here
reads -- so a heuristic that is right almost always beats a dependency.
"""
from __future__ import annotations

import re

# Latin-script languages are separated by their function words, which appear in
# almost any sentence of length and almost never in the others.
MARKERS = {
    "Uzbek": {
        "va", "bilan", "uchun", "qanday", "nima", "men", "sen", "biz", "lekin",
        "yoki", "emas", "kerak", "boshqa", "qilish", "bo'lsa", "bormi", "yaxshi",
        "hammasi", "shuning", "ammo", "faqat", "qachon",
    },
    "Turkish": {
        "ve", "ile", "için", "nasıl", "ne", "ben", "sen", "biz", "ama", "veya",
        "değil", "gerek", "başka", "yapmak", "iyi", "sadece",
    },
    "Spanish": {
        "el", "la", "los", "las", "que", "de", "para", "con", "como", "pero",
        "porque", "cuando", "esto", "hacer", "necesito",
    },
    "German": {
        "der", "die", "das", "und", "ist", "nicht", "für", "mit", "wie", "aber",
        "oder", "ich", "wir", "muss", "soll",
    },
    "French": {
        "le", "la", "les", "et", "est", "pas", "pour", "avec", "comment", "mais",
        "ou", "je", "nous", "faire", "dois",
    },
}

# Scripts are decisive on their own. ORDER MATTERS: Japanese is tested before
# Chinese because Japanese text contains kanji, which share the CJK block --
# testing Chinese first labels every Japanese sentence as Chinese. Kana is
# exclusively Japanese, so it settles the question the other way round.
SCRIPTS = [
    ("Russian", re.compile(r"[а-яёА-ЯЁ]")),
    ("Arabic", re.compile(r"[؀-ۿ]")),
    ("Japanese", re.compile(r"[぀-ヿ]")),
    ("Chinese", re.compile(r"[一-鿿]")),
    ("Korean", re.compile(r"[가-힯]")),
    ("Hebrew", re.compile(r"[֐-׿]")),
    ("Greek", re.compile(r"[Ͱ-Ͽ]")),
    ("Hindi", re.compile(r"[ऀ-ॿ]")),
]

MIN_SCRIPT_RATIO = 0.12     # a stray borrowed word should not flip the verdict


def detect(text: str) -> str:
    """Best guess at the language, defaulting to English.

    English is the default rather than "unknown" because the caller's next move
    is to name a language in a prompt, and naming English when unsure produces
    the same behaviour as saying nothing.
    """
    if not text or not text.strip():
        return "English"

    sample = text[:1200]
    letters = sum(1 for c in sample if c.isalpha())
    if letters:
        for name, pattern in SCRIPTS:
            hits = len(pattern.findall(sample))
            if hits / letters >= MIN_SCRIPT_RATIO:
                return name

    words = set(re.findall(r"[a-zA-ZçğıöşüÇĞİÖŞÜ']+", sample.lower()))
    if not words:
        return "English"

    best, score = "English", 0
    for lang, markers in MARKERS.items():
        hits = len(words & markers)
        if hits > score:
            best, score = lang, hits

    # Two markers, because one shared word ("ve", "la", "das") is a coincidence
    # and flipping a whole answer's language on a coincidence is worse than
    # defaulting to English.
    return best if score >= 2 else "English"


def reply_instruction(language: str) -> str:
    """What to tell the seats whose output the user actually reads."""
    if language == "English":
        return ""
    return (
        f"\nLANGUAGE: the user wrote in {language}. Write your ENTIRE reply in "
        f"{language}, fluently, as a native speaker would -- not translated "
        f"English. Keep code, identifiers, model names and the JSON block "
        f"exactly as they are; those are not prose.\n"
    )


INTERNAL_NOTE = (
    "\nINTERNAL: you are talking to the other advisors, not to the user. "
    "Write in English regardless of the language of the question -- the "
    "reasoning happens in English and only the final reply is translated.\n"
)
