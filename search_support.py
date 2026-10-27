"""Shared search matching for game names and catalog descriptions."""

import unicodedata


def _fold(value: str) -> str:
    decomposed = unicodedata.normalize("NFKD", value.casefold())
    return "".join(character for character in decomposed
                   if not unicodedata.combining(character))


def matches_game_search(name: str, description: str, query: str) -> bool:
    """Match every query word against the game name or its description."""
    terms = _fold(query).split()
    if not terms:
        return True
    searchable_text = _fold(f"{name} {description}")
    return all(term in searchable_text for term in terms)
