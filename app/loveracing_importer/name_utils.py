import re


HORSE_NAME_SUFFIX_RE = re.compile(r"\s*\((?:[A-Z]{2,3}|B\d+)\)", re.I)
HORSE_BREEDING_SUFFIX_RE = re.compile(r"\s*\(\s*\d{1,2}\s*[cfgmhr]\b[^)]*\)\s*$", re.I)
INITIALS_RE = re.compile(r"^(?:[A-Z]\s*){1,3}$", re.I)


def clean(value: str | None) -> str:
    return " ".join((value or "").replace("\xa0", " ").split()).strip()


def clean_horse_name(value: str | None) -> str:
    cleaned = clean(value)
    if not cleaned:
        return ""
    cleaned = HORSE_NAME_SUFFIX_RE.sub(" ", cleaned)
    cleaned = HORSE_BREEDING_SUFFIX_RE.sub(" ", cleaned)
    return clean(cleaned)


def clean_trainer_name(value: str | None) -> str:
    cleaned = clean(value)
    if "," not in cleaned:
        return cleaned
    name, suffix = [clean(part) for part in cleaned.split(",", 1)]
    if name and suffix and re.search(r"[A-Za-z]", suffix):
        return name
    return cleaned


def split_trainer_names(value: str | None) -> list[str]:
    cleaned = clean_trainer_name(value)
    if not cleaned:
        return []
    parts = [clean(part) for part in re.split(r"\s+(?:&|and)\s+", cleaned, flags=re.I) if clean(part)]
    if len(parts) <= 1:
        return parts

    expanded_parts = []
    for index, part in enumerate(parts):
        if is_initials_only(part):
            suffix = _nearest_following_surname(parts[index + 1 :])
            expanded_parts.append(f"{part} {suffix}" if suffix else part)
        else:
            expanded_parts.append(part)
    parts = expanded_parts

    last_tokens = parts[-1].split()
    shared_suffix = " ".join(last_tokens[1:]) if len(last_tokens) > 1 else ""
    if not shared_suffix:
        return parts

    return [f"{part} {shared_suffix}" if len(part.split()) == 1 else part for part in parts[:-1]] + [parts[-1]]


def is_initials_only(value: str | None) -> bool:
    cleaned = clean(value).replace(".", "")
    return bool(cleaned and INITIALS_RE.fullmatch(cleaned))


def is_initial_style_name(value: str | None) -> bool:
    tokens = clean(value).replace(".", "").split()
    if len(tokens) < 2:
        return False
    return all(len(token) == 1 and token.isalpha() for token in tokens[:-1]) and len(tokens[-1]) > 1


def _nearest_following_surname(parts: list[str]) -> str:
    for part in parts:
        tokens = clean(part).split()
        if len(tokens) >= 2 and not is_initials_only(part):
            return tokens[-1]
    return ""
