"""Plain-text helpers shared by the Telegram and Telegraph alert builders."""

import re

# Periods that don't end a sentence in clinical prose.
_ABBREVIATIONS = ("e.g.", "i.e.", "vs.", "et al.", "approx.", "ca.", "Dr.", "Fig.", "No.", "St.")
_SENTENCE_END_RE = re.compile(r"[.!?][\"')\]]*\s+(?=[A-Z0-9\"'(\[])")


def strip_markdown(text: str) -> str:
    """The synthesis is markdown-ish; alerts want plain text."""
    text = re.sub(r"\*\*(.+?)\*\*", r"\1", text)
    text = re.sub(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)", r"\1", text)
    text = re.sub(r"\[([^\]]+)\]\(([^)]+)\)", r"\1 (\2)", text)
    text = re.sub(r"^#+\s*", "", text, flags=re.MULTILINE)
    text = re.sub(r"^[-*]\s+", "", text, flags=re.MULTILINE)
    return text.strip()


def split_sentences(text: str) -> list[str]:
    """Splits prose into whole sentences. Decimals ("0.82") never split because no space
    follows the period; common abbreviations ("vs.", "et al.") are rejoined."""
    text = " ".join(text.split())
    if not text:
        return []
    sentences: list[str] = []
    start = 0
    for match in _SENTENCE_END_RE.finditer(text):
        candidate = text[start : match.start() + 1]
        if candidate.rstrip().endswith(_ABBREVIATIONS):
            continue
        sentences.append(text[start : match.end()].strip())
        start = match.end()
    if start < len(text):
        sentences.append(text[start:].strip())
    return [s for s in sentences if s]


def truncate_at_word(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    cut = text[: max_chars - 1].rsplit(" ", 1)[0].rstrip(" ,;:")
    return cut + "…"
