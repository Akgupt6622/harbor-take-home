from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

import pytest

MODULE_PATH = (
    Path(__file__).resolve().parents[2]
    / "benchmarks"
    / "tau3-bench"
    / "harness"
    / "agent"
    / "write_validator.py"
)


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("write_validator", MODULE_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


wv = _load_module()

ORDER: dict[str, Any] = {
    "order_id": "#W0000001",
    "user_id": "mei_kovacs_8020",
    "status": "delivered",
    "items": [
        {
            "item_id": "1111111111",
            "product_id": "9999999999",
            "name": "Water Bottle",
            "price": 30.0,
            "options": {"color": "red", "capacity": "500ml"},
        },
        {
            "item_id": "2222222222",
            "product_id": "8888888888",
            "name": "Desk Lamp",
            "price": 100.0,
            "options": {"color": "white", "brightness": "high"},
        },
    ],
}

PRODUCT: dict[str, Any] = {
    "product_id": "9999999999",
    "name": "Water Bottle",
    "variants": {
        "3333333333": {
            "item_id": "3333333333",
            "options": {"color": "blue", "capacity": "500ml"},
            "available": True,
            "price": 32.0,
        },
        "4444444444": {
            "item_id": "4444444444",
            "options": {"color": "red", "capacity": "1000ml"},
            "available": False,
            "price": 35.0,
        },
        "5555555555": {
            "item_id": "5555555555",
            "options": {"color": "red", "capacity": "750ml"},
            "available": True,
            "price": 33.0,
        },
    },
}


def make_facts(
    *,
    authenticated: bool = True,
    orders: dict[str, dict[str, Any]] | None = None,
    products: dict[str, dict[str, Any]] | None = None,
    successful_writes: list[tuple[str, dict[str, Any]]] | None = None,
    user_text: str = "",
) -> Any:
    return wv.ConversationFacts(
        authenticated=authenticated,
        orders=orders if orders is not None else {"#W0000001": ORDER},
        products=products if products is not None else {"9999999999": PRODUCT},
        users={},
        successful_writes=successful_writes or [],
        user_text=user_text.lower(),
    )


class TestV5AuthFirst:
    def test_bounces_write_before_auth(self) -> None:
        bounce = wv.validate_write(
            "cancel_pending_order",
            {"order_id": "#W0000001", "reason": "no longer needed"},
            make_facts(authenticated=False),
        )
        assert bounce is not None
        assert "authenticate" in bounce.lower()

    def test_passes_write_after_auth(self) -> None:
        assert (
            wv.validate_write(
                "cancel_pending_order",
                {"order_id": "#W0000001", "reason": "no longer needed"},
                make_facts(),
            )
            is None
        )

    def test_ignores_non_write_tools(self) -> None:
        assert (
            wv.validate_write(
                "get_order_details",
                {"order_id": "#W0000001"},
                make_facts(authenticated=False),
            )
            is None
        )


class TestV1ItemInOrder:
    def test_bounces_when_order_not_fetched(self) -> None:
        bounce = wv.validate_write(
            "return_delivered_order_items",
            {
                "order_id": "#W7777777",
                "item_ids": ["1111111111"],
                "payment_method_id": "gift_card_1",
            },
            make_facts(orders={}),
        )
        assert bounce is not None
        assert "get_order_details" in bounce
        assert "#W7777777" in bounce

    def test_bounces_when_item_not_in_order(self) -> None:
        bounce = wv.validate_write(
            "return_delivered_order_items",
            {
                "order_id": "#W0000001",
                "item_ids": ["1111111111", "6666666666"],
                "payment_method_id": "gift_card_1",
            },
            make_facts(),
        )
        assert bounce is not None
        assert "6666666666" in bounce
        assert "not in order #W0000001" in bounce
        assert "get_order_details" in bounce

    def test_passes_when_all_items_in_order(self) -> None:
        assert (
            wv.validate_write(
                "return_delivered_order_items",
                {
                    "order_id": "#W0000001",
                    "item_ids": ["1111111111", "2222222222"],
                    "payment_method_id": "gift_card_1",
                },
                make_facts(),
            )
            is None
        )


class TestV2VariantValidity:
    def test_bounces_when_product_not_fetched(self) -> None:
        bounce = wv.validate_write(
            "exchange_delivered_order_items",
            {
                "order_id": "#W0000001",
                "item_ids": ["1111111111"],
                "new_item_ids": ["3333333333"],
                "payment_method_id": "gift_card_1",
            },
            make_facts(products={}),
        )
        assert bounce is not None
        assert "get_product_details" in bounce
        assert "9999999999" in bounce

    def test_bounces_when_new_item_not_a_variant(self) -> None:
        bounce = wv.validate_write(
            "exchange_delivered_order_items",
            {
                "order_id": "#W0000001",
                "item_ids": ["1111111111"],
                "new_item_ids": ["1234567890"],
                "payment_method_id": "gift_card_1",
            },
            make_facts(user_text="anything"),
        )
        assert bounce is not None
        assert "not a variant" in bounce

    def test_bounces_when_variant_unavailable(self) -> None:
        bounce = wv.validate_write(
            "exchange_delivered_order_items",
            {
                "order_id": "#W0000001",
                "item_ids": ["1111111111"],
                "new_item_ids": ["4444444444"],
                "payment_method_id": "gift_card_1",
            },
            make_facts(user_text="I want the 1000ml one"),
        )
        assert bounce is not None
        assert "available" in bounce

    def test_bounces_on_list_length_mismatch(self) -> None:
        bounce = wv.validate_write(
            "exchange_delivered_order_items",
            {
                "order_id": "#W0000001",
                "item_ids": ["1111111111"],
                "new_item_ids": ["3333333333", "5555555555"],
                "payment_method_id": "gift_card_1",
            },
            make_facts(),
        )
        assert bounce is not None
        assert "same position" in bounce

    def test_passes_available_variant_of_same_product(self) -> None:
        assert (
            wv.validate_write(
                "exchange_delivered_order_items",
                {
                    "order_id": "#W0000001",
                    "item_ids": ["1111111111"],
                    "new_item_ids": ["5555555555"],
                    "payment_method_id": "gift_card_1",
                },
                make_facts(user_text="please switch it to the 750ml size"),
            )
            is None
        )


class TestV3OptionPreservation:
    def test_bounces_when_changed_value_not_mentioned(self) -> None:
        bounce = wv.validate_write(
            "exchange_delivered_order_items",
            {
                "order_id": "#W0000001",
                "item_ids": ["1111111111"],
                "new_item_ids": ["3333333333"],
                "payment_method_id": "gift_card_1",
            },
            make_facts(user_text="I want to exchange my water bottle please"),
        )
        assert bounce is not None
        assert "'blue'" in bounce
        assert "confirm" in bounce.lower()

    def test_passes_when_user_mentioned_changed_value(self) -> None:
        assert (
            wv.validate_write(
                "exchange_delivered_order_items",
                {
                    "order_id": "#W0000001",
                    "item_ids": ["1111111111"],
                    "new_item_ids": ["3333333333"],
                    "payment_method_id": "gift_card_1",
                },
                make_facts(user_text="Could I get something in blue instead?"),
            )
            is None
        )

    def test_mention_matching_ignores_case_and_separators(self) -> None:
        assert (
            wv.validate_write(
                "exchange_delivered_order_items",
                {
                    "order_id": "#W0000001",
                    "item_ids": ["1111111111"],
                    "new_item_ids": ["4444444444"],
                    "payment_method_id": "gift_card_1",
                },
                make_facts(
                    products={
                        "9999999999": {
                            **PRODUCT,
                            "variants": {
                                "4444444444": {
                                    **PRODUCT["variants"]["4444444444"],
                                    "available": True,
                                }
                            },
                        }
                    },
                    user_text="Give me the 1,000 ml bottle",
                ),
            )
            is None
        )

    def test_applies_to_modify_pending_order_items(self) -> None:
        bounce = wv.validate_write(
            "modify_pending_order_items",
            {
                "order_id": "#W0000001",
                "item_ids": ["1111111111"],
                "new_item_ids": ["3333333333"],
                "payment_method_id": "gift_card_1",
            },
            make_facts(user_text="change my bottle to whatever is cheapest"),
        )
        assert bounce is not None
        assert "'blue'" in bounce


class TestV4Sequencing:
    def test_bounces_second_return_or_exchange_on_same_order(self) -> None:
        facts = make_facts(
            successful_writes=[
                (
                    "return_delivered_order_items",
                    {"order_id": "#W0000001", "item_ids": ["2222222222"]},
                )
            ],
            user_text="something in blue",
        )
        bounce = wv.validate_write(
            "exchange_delivered_order_items",
            {
                "order_id": "#W0000001",
                "item_ids": ["1111111111"],
                "new_item_ids": ["3333333333"],
                "payment_method_id": "gift_card_1",
            },
            facts,
        )
        assert bounce is not None
        assert "only one" in bounce

    def test_passes_return_on_different_order(self) -> None:
        other_order = {**ORDER, "order_id": "#W0000002"}
        facts = make_facts(
            orders={"#W0000001": ORDER, "#W0000002": other_order},
            successful_writes=[
                (
                    "return_delivered_order_items",
                    {"order_id": "#W0000001", "item_ids": ["2222222222"]},
                )
            ],
        )
        assert (
            wv.validate_write(
                "return_delivered_order_items",
                {
                    "order_id": "#W0000002",
                    "item_ids": ["1111111111"],
                    "payment_method_id": "gift_card_1",
                },
                facts,
            )
            is None
        )

    def test_bounces_address_change_after_item_modify_lock(self) -> None:
        facts = make_facts(
            successful_writes=[
                ("modify_pending_order_items", {"order_id": "#W0000001"})
            ]
        )
        bounce = wv.validate_write(
            "modify_pending_order_address",
            {"order_id": "#W0000001", "address1": "1 Main St"},
            facts,
        )
        assert bounce is not None
        assert "BEFORE" in bounce

    def test_bounces_payment_change_after_item_modify_lock(self) -> None:
        facts = make_facts(
            successful_writes=[
                ("modify_pending_order_items", {"order_id": "#W0000001"})
            ]
        )
        bounce = wv.validate_write(
            "modify_pending_order_payment",
            {"order_id": "#W0000001", "payment_method_id": "gift_card_1"},
            facts,
        )
        assert bounce is not None
        assert "locked" in bounce

    def test_passes_address_change_before_item_modify(self) -> None:
        assert (
            wv.validate_write(
                "modify_pending_order_address",
                {"order_id": "#W0000001", "address1": "1 Main St"},
                make_facts(),
            )
            is None
        )

    def test_passes_address_change_when_other_order_locked(self) -> None:
        facts = make_facts(
            successful_writes=[
                ("modify_pending_order_items", {"order_id": "#W0000002"})
            ]
        )
        assert (
            wv.validate_write(
                "modify_pending_order_address",
                {"order_id": "#W0000001", "address1": "1 Main St"},
                facts,
            )
            is None
        )


class TestCollectFacts:
    def _conversation(self) -> list[dict[str, Any]]:
        return [
            {"role": "user", "content": "Hi, I want something in blue."},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "name": "find_user_id_by_name_zip",
                        "arguments": {
                            "first_name": "Mei",
                            "last_name": "Kovacs",
                            "zip": "28236",
                        },
                    }
                ],
            },
            {"role": "tool", "id": "c1", "content": "mei_kovacs_8020", "error": False},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c2",
                        "name": "get_order_details",
                        "arguments": {"order_id": "#W0000001"},
                    }
                ],
            },
            {"role": "tool", "id": "c2", "content": json.dumps(ORDER), "error": False},
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c3",
                        "name": "get_product_details",
                        "arguments": {"product_id": "9999999999"},
                    }
                ],
            },
            {
                "role": "tool",
                "id": "c3",
                "content": json.dumps(PRODUCT),
                "error": False,
            },
        ]

    def test_builds_caches_auth_and_user_text(self) -> None:
        facts = wv.collect_facts(self._conversation())
        assert facts.authenticated is True
        assert "#W0000001" in facts.orders
        assert "9999999999" in facts.products
        assert "blue" in facts.user_text

    def test_errored_tool_results_are_ignored(self) -> None:
        conversation = [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c1",
                        "name": "find_user_id_by_email",
                        "arguments": {"email": "x@example.com"},
                    }
                ],
            },
            {
                "role": "tool",
                "id": "c1",
                "content": "Error: User not found",
                "error": True,
            },
        ]
        facts = wv.collect_facts(conversation)
        assert facts.authenticated is False

    def test_write_results_update_order_cache_and_history(self) -> None:
        returned = {**ORDER, "status": "return requested"}
        conversation = self._conversation() + [
            {
                "role": "assistant",
                "content": None,
                "tool_calls": [
                    {
                        "id": "c4",
                        "name": "return_delivered_order_items",
                        "arguments": {
                            "order_id": "#W0000001",
                            "item_ids": ["2222222222"],
                            "payment_method_id": "gift_card_1",
                        },
                    }
                ],
            },
            {
                "role": "tool",
                "id": "c4",
                "content": json.dumps(returned),
                "error": False,
            },
        ]
        facts = wv.collect_facts(conversation)
        assert facts.successful_writes == [
            (
                "return_delivered_order_items",
                {
                    "order_id": "#W0000001",
                    "item_ids": ["2222222222"],
                    "payment_method_id": "gift_card_1",
                },
            )
        ]
        assert facts.orders["#W0000001"]["status"] == "return requested"


class TestBounceToolCalls:
    def test_empty_when_all_calls_valid(self) -> None:
        conversation = TestCollectFacts()._conversation()
        assert (
            wv.bounce_tool_calls(
                [
                    {
                        "id": "w1",
                        "name": "exchange_delivered_order_items",
                        "arguments": {
                            "order_id": "#W0000001",
                            "item_ids": ["1111111111"],
                            "new_item_ids": ["3333333333"],
                            "payment_method_id": "gift_card_1",
                        },
                    }
                ],
                conversation,
            )
            == {}
        )

    def test_all_calls_answered_when_any_violation(self) -> None:
        conversation = TestCollectFacts()._conversation()
        bounces = wv.bounce_tool_calls(
            [
                {
                    "id": "w1",
                    "name": "return_delivered_order_items",
                    "arguments": {
                        "order_id": "#W0000001",
                        "item_ids": ["6666666666"],
                        "payment_method_id": "gift_card_1",
                    },
                },
                {
                    "id": "w2",
                    "name": "get_order_details",
                    "arguments": {"order_id": "#W0000001"},
                },
            ],
            conversation,
        )
        assert set(bounces) == {"w1", "w2"}
        assert bounces["w1"].startswith("VALIDATOR:")
        assert bounces["w2"] == wv.SKIPPED_CALL_MESSAGE

    def test_read_only_calls_pass_through(self) -> None:
        assert (
            wv.bounce_tool_calls(
                [
                    {
                        "id": "r1",
                        "name": "get_user_details",
                        "arguments": {"user_id": "mei_kovacs_8020"},
                    }
                ],
                [],
            )
            == {}
        )


@pytest.mark.parametrize(
    "tool_name",
    sorted(
        {
            "cancel_pending_order",
            "exchange_delivered_order_items",
            "modify_pending_order_address",
            "modify_pending_order_items",
            "modify_pending_order_payment",
            "modify_user_address",
            "return_delivered_order_items",
        }
    ),
)
def test_write_tool_registry_matches_retail_toolkit(tool_name: str) -> None:
    assert tool_name in wv.WRITE_TOOL_NAMES


def test_bounced_write_does_not_enter_history() -> None:
    conversation = [
        {"role": "user", "content": "please exchange my camera", "tool_calls": []},
        {
            "role": "tool",
            "id": "a1",
            "content": "sofia_li_9219",
            "error": False,
            "tool_calls": [],
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "a1x",
                    "name": "find_user_id_by_name_zip",
                    "arguments": {"first_name": "S", "last_name": "L", "zip": "1"},
                }
            ],
        },
        {
            "role": "tool",
            "id": "a1x",
            "content": "sofia_li_9219",
            "error": False,
            "tool_calls": [],
        },
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [
                {
                    "id": "b2",
                    "name": "exchange_delivered_order_items",
                    "arguments": {
                        "order_id": "#W1",
                        "item_ids": ["i1"],
                        "new_item_ids": ["i2"],
                        "payment_method_id": "pm",
                    },
                }
            ],
        },
        {
            "role": "tool",
            "id": "b2",
            "content": "VALIDATOR: Order #W1 has not been fetched in this conversation; "
            "call get_order_details first.",
            "error": False,
            "tool_calls": [],
        },
    ]
    facts = wv.collect_facts(conversation)
    assert facts.successful_writes == []
    assert facts.authenticated is True


def test_item_bounce_names_the_order_that_has_it() -> None:
    facts = wv.ConversationFacts()
    facts.authenticated = True
    facts.orders["#W1"] = {"order_id": "#W1", "items": [{"item_id": "boots"}]}
    facts.orders["#W2"] = {"order_id": "#W2", "items": [{"item_id": "puzzle"}]}
    bounce = wv.validate_write(
        "exchange_delivered_order_items",
        {
            "order_id": "#W2",
            "item_ids": ["boots"],
            "new_item_ids": ["boots2"],
            "payment_method_id": "pm",
        },
        facts,
    )
    assert bounce is not None
    assert "IS in your already-fetched order #W1" in bounce
    assert "consumed nothing" in bounce
