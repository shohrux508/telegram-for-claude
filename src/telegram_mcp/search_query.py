"""Pure text helpers for archive search: normalisation, FTS5 query building, snippets.

A copy of core's modules/tg_archive/query.py: the local archive and the one on
core must find the same messages for the same query. Change both together.

The same `normalize` runs over message text at index time and over the query at
search time, so both sides always agree on case and on ё/е.
"""

from __future__ import annotations

import re

_TOKEN = re.compile(r'"([^"]+)"|(\w+)')
_WORD = re.compile(r"\w+")
_CYRILLIC = re.compile(r"[а-я]+")
# Russian inflectional endings, longest first. Stripping one turns a word into a
# prefix that matches its other forms: "договора" -> "договор*", "цены" -> "цен*".
# Deliberately crude: recall over precision, ranking puts exact forms first.
_ENDING = re.compile(
    r"(?:ами|ями|ого|его|ому|ему|ыми|ими"
    r"|ах|ях|ов|ев|ей|ой|ий|ый|ая|яя|ое|ее|ые|ие|ую|юю|ом|ем|ам|ям"
    r"|[аяоеыиуюьй])$"
)
_MIN_STEM = 3
_SNIPPET_WIDTH = 240


def normalize(text: str) -> str:
    """Lowercase and fold ё into е: people type "еще" and "ещё" interchangeably."""
    return text.lower().replace("ё", "е")


def stem(word: str) -> str:
    """Strip one Russian ending from an already normalised word; other scripts pass through.

    Latin-script words (English, Uzbek) are left whole: their forms add suffixes,
    so the prefix query built from the full word already matches them.
    """
    if not _CYRILLIC.fullmatch(word):
        return word
    stripped = _ENDING.sub("", word, count=1)
    return stripped if len(stripped) >= _MIN_STEM else word


def query_terms(query: str) -> list[str]:
    """The stems and phrase words a query looks for, normalised. Used to place snippets."""
    terms: list[str] = []
    for phrase, word in _TOKEN.findall(normalize(query)):
        terms.extend(_WORD.findall(phrase) if phrase else [stem(word)])
    return terms


def build_match_query(query: str) -> str | None:
    """Translate user input into an FTS5 MATCH expression, or None if it has no words.

    Bare words become prefix terms joined by AND; "quoted text" is an exact phrase.
    Only \\w characters ever reach the expression, so input cannot inject FTS syntax.
    """
    parts: list[str] = []
    for phrase, word in _TOKEN.findall(normalize(query)):
        if phrase:
            words = _WORD.findall(phrase)
            if words:
                parts.append('"' + " ".join(words) + '"')
        else:
            parts.append(f'"{stem(word)}"*')
    return " ".join(parts) or None


def make_snippet(text: str, query: str, width: int = _SNIPPET_WIDTH) -> str:
    """Return *text* whole if short, otherwise a window around the first matched term."""
    if len(text) <= width:
        return text
    haystack = normalize(text)
    found = [pos for term in query_terms(query) if (pos := haystack.find(term)) >= 0]
    start = max(0, min(found, default=0) - width // 3)
    end = min(len(text), start + width)
    return ("…" if start else "") + text[start:end].strip() + ("…" if end < len(text) else "")
