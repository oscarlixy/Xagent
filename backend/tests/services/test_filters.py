from x_digest.services.filters import FilterRules, should_include


def test_default_filter_excludes_pure_reposts_and_keeps_original_posts() -> None:
    assert should_include(text="useful", reference_types=(), rules=FilterRules()) is True
    assert should_include(text="", reference_types=("reposted",), rules=FilterRules()) is False


def test_blacklist_wins_over_default_inclusion() -> None:
    assert (
        should_include(
            text="sponsored offer",
            reference_types=(),
            rules=FilterRules(blacklist=("sponsored",)),
        )
        is False
    )
