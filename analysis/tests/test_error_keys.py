"""Runtime error-key table derivation pinned to the committed harness source.

Run: uv run pytest analysis/tests -q
"""

from __future__ import annotations

from pathlib import Path

import pytest

from analysis.error_keys import ErrorKeyTable, load_error_keys
from analysis.parse_traces import HARNESS_PACKAGE_ROOT

REPO_ROOT = Path(__file__).resolve().parents[2]
TOOLS_PATH = REPO_ROOT / HARNESS_PACKAGE_ROOT / "harness" / "retail" / "tools.py"


def test_table_derived_from_harness_source() -> None:
    table = load_error_keys(TOOLS_PATH)
    assert table.match("Error: User not found") == "user_not_found"
    assert table.match("Item 6086499569 not found") == "item_x_not_found"
    assert table.raise_site_count == 33
    assert len(table.patterns) == 26


def test_table_is_cached_per_resolved_path() -> None:
    aliased = TOOLS_PATH.parent / ".." / "retail" / "tools.py"
    assert load_error_keys(TOOLS_PATH) is load_error_keys(aliased)


def test_non_literal_raise_argument_aborts_loudly(tmp_path: Path) -> None:
    tools = tmp_path / "tools.py"
    tools.write_text(
        "def cancel(some_variable: str) -> None:\n    raise ValueError(some_variable)\n"
    )
    with pytest.raises(
        SystemExit, match=r"non-literal ValueError messages at lines \[2\]"
    ):
        load_error_keys(tools)


def test_most_literal_pattern_wins() -> None:
    table = load_error_keys(TOOLS_PATH)
    assert table.match("Order not found") == "order_not_found"
    assert table.match("gift cards not found") == "x_not_found"
    assert table.match("New item 123 not found or available") == (
        "new_item_x_not_found_or_available"
    )


def test_trailing_period_variants_stay_distinct() -> None:
    table = load_error_keys(TOOLS_PATH)
    assert table.match("The number of items to be exchanged should match") == (
        "the_number_of_items_to_be_exchanged_should_match"
    )
    assert table.match("The number of items to be exchanged should match.") == (
        "the_number_of_items_to_be_exchanged_should_match_2"
    )
    assert table.match("Number of gift cards not found.") == "number_of_x_not_found"


def test_zero_match_raises_lookup_error() -> None:
    table = load_error_keys(TOOLS_PATH)
    with pytest.raises(LookupError, match="no unambiguous error-key match"):
        table.match("Flux capacitor misaligned")


def test_literal_length_tie_raises_lookup_error() -> None:
    table = ErrorKeyTable(
        source=Path("synthetic"),
        patterns=(("a_x", "a(.+?)"), ("x_a", "(.+?)a")),
        raise_site_count=2,
    )
    with pytest.raises(LookupError, match="no unambiguous error-key match"):
        table.match("aba")
