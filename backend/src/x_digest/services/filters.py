from dataclasses import dataclass


@dataclass(frozen=True)
class FilterRules:
    blacklist: tuple[str, ...] = ()


def should_include(*, text: str, reference_types: tuple[str, ...], rules: FilterRules) -> bool:
    normalized_text = text.casefold()
    if any(keyword.casefold() in normalized_text for keyword in rules.blacklist):
        return False
    return not (reference_types and set(reference_types) == {"reposted"})
