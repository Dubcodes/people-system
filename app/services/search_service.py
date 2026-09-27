import re
from collections.abc import Iterable


def normalize_search_text(value: str | None) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", (value or "").lower()).split())


def search_value_matches(value: str | None, search: str | None) -> bool:
    candidate = normalize_search_text(value)
    query = normalize_search_text(search)
    if not query:
        return True
    if not candidate:
        return False
    if query in candidate:
        return True

    candidate_tokens = candidate.split()
    query_tokens = query.split()
    candidate_index = 0
    for query_token in query_tokens:
        while candidate_index < len(candidate_tokens) and not candidate_tokens[candidate_index].startswith(query_token):
            candidate_index += 1
        if candidate_index >= len(candidate_tokens):
            return False
        candidate_index += 1
    return True


def values_match_search(values: Iterable[str | None], search: str | None) -> bool:
    return any(search_value_matches(value, search) for value in values)
