"""Deterministic turn-tier router.

The router is deliberately local and cheap: no model call, no embedding, no
state. It only decides how much tool context a turn needs. Uncertain input
falls back toward the safer, more capable tier.
"""

from __future__ import annotations

import re
from typing import Literal

TurnTier = Literal["fast", "agent", "deep"]

_FAST_MAX_CHARS = 140
_DEEP_LONG_CHARS = 500

_EXPLICIT_DEEP = re.compile(
    r"(?<!\w)(?:"
    r"refactor(?:ing|ed|s)?|architect(?:ing|ed|s|ure)?|"
    r"migrat(?:e|ed|ing|ion|ions)|audit(?:ing|ed|s)?|"
    r"implement(?:ing|ed|s|ation)?|multi[-\s]?step|plan(?:s|ned|ning)?|"
    r"refaktor(?:ieren|ierte|iert|ierung)?|architekt(?:ur|en|isch)?|"
    r"migration(?:en)?|implementier(?:e|en|t|ung)?|umsetz(?:en|ung|t)?|"
    r"mehrschritt(?:ig|e)?|plane(?:n|st|t)"
    r")(?!\w)",
    re.IGNORECASE,
)

_URL = re.compile(r"https?://", re.IGNORECASE)
_WINDOWS_PATH = re.compile(r"(?<!\w)[a-z]:[\\/]", re.IGNORECASE)
_POSIX_PATH = re.compile(r"(?<!\S)(?:\./|\.\./|~/|/[^\s]+)")
_PATHLIKE_TOKEN = re.compile(
    r"(?<!\w)(?:src|test|tests|runner|app|lib|bin|scripts?|docs?)/[^\s]*",
    re.IGNORECASE,
)
_FILE_EXTENSION = re.compile(
    r"(?<!\w)[\w.,@+-]*\.(?:py|ts|tsx|js|jsx|json|jsonl|md|markdown|yaml|yml|toml|rs|go|java|cs|cpp|c|h|sh|ps1|sql|html|css)(?!\w)",
    re.IGNORECASE,
)
_CODE_SHAPE = re.compile(
    r"(?<!\w)(?:def|class|function|const|let|var|import|export|interface|struct|enum)\s+\w+",
    re.IGNORECASE,
)

_ACTION_WORDS = re.compile(
    r"(?<!\w)(?:"
    r"read|write|edit|run|exec|execute|fix|debug|build|test|commit|push|git|"
    r"search|find|grep|list|open|browse|scrape|calendar|schedule|remember|memory|"
    r"save|delete|install|deploy|refactor|inspect|check|file|folder|repo|branch|"
    r"pr|issue|board|terminal"
    r")(?:s|ed)?(?!\w)|"
    r"(?<!\w)(?:lies|schreib|such|prüf|pruef|bau|merk|speicher|lösch|loesch)\w*|"
    r"(?<!\w)(?:datei|ordner|kalender)(?!\w)|"
    r"zeig\s+mir",
    re.IGNORECASE,
)

_QUESTION_STARTS = {
    "what", "why", "how", "when", "where", "who", "which", "whose", "whom",
    "is", "are", "was", "were", "can", "could", "would", "should", "do", "does", "did",
    "was", "warum", "wieso", "wie", "wann", "wo", "wer", "wen", "wem", "welche", "welcher", "welches",
    "ist", "sind", "kann", "kannst", "können", "könntest", "bist", "hast", "haben",
}

_CONVERSATIONAL_PHRASES = {
    # English greetings and smalltalk.
    "hi", "hi there", "hello", "hello there", "hey", "hey there", "hiya", "howdy", "yo",
    "good morning", "good afternoon", "good evening", "morning", "evening",
    "how are you", "how are you doing", "hows it going", "how is it going", "whats up", "what is up",
    "who are you", "what are you",
    "hi how are you", "hey how are you", "hello how are you",
    # English thanks / acknowledgement.
    "thanks", "thank you", "thanks a lot", "thank you very much", "thx", "ty",
    "ok", "okay", "kk", "cool", "nice", "great", "awesome", "perfect", "fine",
    "got it", "understood", "makes sense", "sounds good", "all right", "alright",
    "yes", "yeah", "yep", "no", "nope", "bye", "goodbye", "see you", "good night",
    # German greetings and smalltalk.
    "hallo", "hallo du", "moin", "moin moin", "servus", "na", "guten morgen", "guten tag",
    "guten abend", "wie gehts", "wie geht es", "wie geht es dir", "wie geht es dir heute",
    "wie geht es ihnen", "wer bist du", "was bist du", "hallo wie gehts", "hi wie gehts",
    "moin wie gehts", "servus wie gehts",
    # German thanks / acknowledgement.
    "danke", "danke schön", "danke sehr", "danke vielmals", "vielen dank", "dankeschön",
    "ok", "okay", "cool", "nice", "super", "prima", "stark", "gut", "sehr gut",
    "alles klar", "verstanden", "passt", "passt so", "ja", "nein", "jep", "tschüss", "ciao",
}


def _clean_words(text: str) -> str:
    """Lowercase word form for exact smalltalk matching."""
    text = text.strip().lower().replace("'", "").replace("’", "").replace("`", "")
    text = re.sub(r"[^\w\s]+", " ", text, flags=re.UNICODE)
    return re.sub(r"\s+", " ", text).strip()


def _normalized(prompt: str) -> str:
    return re.sub(r"\s+", " ", (prompt or "").strip().lower())


def _line_count(prompt: str) -> int:
    stripped = (prompt or "").strip()
    if not stripped:
        return 0
    return len(stripped.splitlines())


def _has_workspace_or_code_reference(prompt: str) -> bool:
    text = prompt or ""
    if not any(marker in text for marker in ("`", "@", "/", "\\", ".", ":")):
        return False
    return bool(
        "```" in text
        or _URL.search(text)
        or "@" in text
        or _WINDOWS_PATH.search(text)
        or _POSIX_PATH.search(text)
        or _PATHLIKE_TOKEN.search(text)
        or _FILE_EXTENSION.search(text)
    )


def _has_action_intent(text: str) -> bool:
    return bool(_ACTION_WORDS.search(text))


def _conversational(text: str) -> bool:
    return _clean_words(text) in _CONVERSATIONAL_PHRASES


_CONCEPTUAL_START = re.compile(
    r"^(?:explain|define|describe|meaning of|was ist|was bedeutet|erklaere|erkläre|definiere)\b",
    re.IGNORECASE,
)


def _short_self_contained_question(prompt: str, text: str) -> bool:
    stripped = (prompt or "").strip()
    if not stripped:
        return False
    clean = _clean_words(text)
    first = clean.split(" ", 1)[0] if clean else ""
    looks_like_question = (
        stripped.endswith("?")
        or first in _QUESTION_STARTS
        or bool(_CONCEPTUAL_START.search(clean))
    )
    return looks_like_question and not _has_action_intent(text)


def _fast_candidate(prompt: str) -> bool:
    text = _normalized(prompt)
    if not text or len(text) > _FAST_MAX_CHARS or _line_count(prompt) > 2:
        return False
    if _has_workspace_or_code_reference(prompt) or _has_action_intent(text):
        return False
    return _conversational(text) or _short_self_contained_question(prompt, text)


def _deep_candidate(prompt: str) -> bool:
    text = prompt or ""
    lowered = text.lower()
    deep_markers = (
        "refactor", "architect", "migrat", "audit", "implement", "multi-step",
        "multi step", "plan", "refaktor", "architekt", "migration", "implementier",
        "umsetz", "mehrschritt",
    )
    if any(marker in lowered for marker in deep_markers) and _EXPLICIT_DEEP.search(text):
        return True
    if len(text.strip()) <= _DEEP_LONG_CHARS:
        return False
    code_keywords = (
        "def ", "class ", "function ", "const ", "let ", "var ", "import ",
        "export ", "interface ", "struct ", "enum ",
    )
    has_code_shape = any(keyword in lowered for keyword in code_keywords) and bool(
        _CODE_SHAPE.search(text)
    )
    if _has_workspace_or_code_reference(text) or has_code_shape:
        return True
    file_words = ("code", "coding", "file", "files", "folder", "repo", "repository", "architecture", "database", "schema", "api")
    if not any(word in lowered for word in file_words):
        return False
    return bool(
        re.search(
            r"(?<!\w)(?:code|coding|file|files|folder|repo|repository|architecture|database|schema|api)(?!\w)",
            text,
            re.IGNORECASE,
        )
    )


def classify_turn(
    prompt: str,
    *,
    mode: str = "auto",
    has_attachments: bool = False,
    history_has_tools: bool = False,
) -> TurnTier:
    """Classify one user turn into ``fast``, ``agent``, or ``deep``.

    ``fast`` is intentionally conservative. It is only chosen for a short,
    self-contained conversational turn with no attachment, no recent tool
    context, no path/code/URL reference, and no action-intent vocabulary.
    Everything uncertain falls through to ``agent``; explicit multi-step or
    architecture-shaped work becomes ``deep``.
    """
    token = (mode or "auto").strip().lower()
    if token in ("fast", "agent", "deep"):
        return token  # type: ignore[return-value]
    if token != "auto":
        return "agent"
    if not has_attachments and not history_has_tools and _fast_candidate(prompt):
        return "fast"
    if _deep_candidate(prompt):
        return "deep"
    return "agent"
