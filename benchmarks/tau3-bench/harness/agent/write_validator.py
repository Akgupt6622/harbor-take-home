"""Agent-side validation of retail write tool calls before execution.

Self-contained (stdlib only): callers pass conversation history as plain
dicts with keys role, content, tool_calls (id/name/arguments), id, error.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

AUTH_TOOL_NAMES: frozenset[str] = frozenset(
    {"find_user_id_by_email", "find_user_id_by_name_zip"}
)
ORDER_ONESHOT_TOOL_NAMES: frozenset[str] = frozenset(
    {"return_delivered_order_items", "exchange_delivered_order_items"}
)
ITEM_LIST_TOOL_NAMES: frozenset[str] = ORDER_ONESHOT_TOOL_NAMES | {
    "modify_pending_order_items"
}
REPLACEMENT_TOOL_NAMES: frozenset[str] = frozenset(
    {"exchange_delivered_order_items", "modify_pending_order_items"}
)
LOCKABLE_FOLLOWUP_TOOL_NAMES: frozenset[str] = frozenset(
    {"modify_pending_order_address", "modify_pending_order_payment"}
)
WRITE_TOOL_NAMES: frozenset[str] = (
    ITEM_LIST_TOOL_NAMES
    | LOCKABLE_FOLLOWUP_TOOL_NAMES
    | {"cancel_pending_order", "modify_user_address"}
)

SKIPPED_CALL_MESSAGE: str = (
    "VALIDATOR: This call was not executed because another tool call in the "
    "same message was rejected. Re-issue this call by itself once the "
    "rejected call is resolved."
)

_SQUASH_PATTERN: re.Pattern[str] = re.compile(r"[\s,\-_]+")


@dataclass
class ConversationFacts:
    """State derived from the conversation so far."""

    authenticated: bool = False
    orders: dict[str, dict[str, Any]] = field(default_factory=dict)
    products: dict[str, dict[str, Any]] = field(default_factory=dict)
    users: dict[str, dict[str, Any]] = field(default_factory=dict)
    successful_writes: list[tuple[str, dict[str, Any]]] = field(default_factory=list)
    user_text: str = ""


def _parse_payload(content: Any) -> Optional[dict[str, Any]]:
    if not isinstance(content, str):
        return None
    try:
        payload = json.loads(content)
    except ValueError:
        return None
    return payload if isinstance(payload, dict) else None


def collect_facts(conversation: Sequence[Mapping[str, Any]]) -> ConversationFacts:
    """Build the read-cache, auth flag, and write history from the history."""
    facts = ConversationFacts()
    calls: dict[str, tuple[str, dict[str, Any]]] = {}
    user_texts: list[str] = []
    for message in conversation:
        role = message.get("role")
        if role == "user" and message.get("content"):
            user_texts.append(str(message["content"]))
        for call in message.get("tool_calls") or []:
            calls[str(call.get("id") or "")] = (
                str(call.get("name") or ""),
                dict(call.get("arguments") or {}),
            )
        if role != "tool" or message.get("error"):
            continue
        matched = calls.get(str(message.get("id") or ""))
        if matched is None:
            continue
        content = message.get("content")
        if isinstance(content, str) and content.startswith("VALIDATOR:"):
            continue  # bounced calls were never executed; they must not enter history
        name, arguments = matched
        if name in AUTH_TOOL_NAMES:
            facts.authenticated = True
        if name in WRITE_TOOL_NAMES:
            facts.successful_writes.append((name, arguments))
        payload = _parse_payload(message.get("content"))
        if payload is None:
            continue
        if "order_id" in payload and "items" in payload:
            facts.orders[str(payload["order_id"])] = payload
        elif "product_id" in payload and "variants" in payload:
            facts.products[str(payload["product_id"])] = payload
        elif "user_id" in payload and "payment_methods" in payload:
            facts.users[str(payload["user_id"])] = payload
    facts.user_text = "\n".join(user_texts).lower()
    return facts


def _mentioned_by_user(value: str, user_text: str) -> bool:
    needle = value.lower()
    if needle in user_text:
        return True
    squashed_needle = _SQUASH_PATTERN.sub("", needle)
    squashed_text = _SQUASH_PATTERN.sub("", user_text)
    return bool(squashed_needle) and squashed_needle in squashed_text


def bounce_tool_calls(
    tool_calls: Sequence[Mapping[str, Any]],
    conversation: Sequence[Mapping[str, Any]],
) -> dict[str, str]:
    """Map tool-call id to bounce text for every call, or {} if all may run."""
    if not any(str(call.get("name") or "") in WRITE_TOOL_NAMES for call in tool_calls):
        return {}
    facts = collect_facts(conversation)
    violations = {
        str(call.get("id") or ""): bounce
        for call in tool_calls
        if (
            bounce := validate_write(
                str(call.get("name") or ""), dict(call.get("arguments") or {}), facts
            )
        )
        is not None
    }
    if not violations:
        return {}
    return {
        call_id: violations.get(call_id, SKIPPED_CALL_MESSAGE)
        for call_id in (str(call.get("id") or "") for call in tool_calls)
    }


def validate_write(
    tool_name: str,
    arguments: Mapping[str, Any],
    facts: ConversationFacts,
) -> Optional[str]:
    """Return a corrective bounce message, or None if the call may proceed."""
    if tool_name not in WRITE_TOOL_NAMES:
        return None
    if not facts.authenticated:
        return (
            "VALIDATOR: No user has been authenticated in this conversation "
            "(no successful find_user_id_by_email or find_user_id_by_name_zip "
            "result). Authenticate the user first, then retry this action."
        )
    order_id = str(arguments.get("order_id") or "")
    if tool_name in ORDER_ONESHOT_TOOL_NAMES and any(
        name in ORDER_ONESHOT_TOOL_NAMES and str(args.get("order_id") or "") == order_id
        for name, args in facts.successful_writes
    ):
        return (
            f"VALIDATOR: A return or exchange has already been submitted for "
            f"order {order_id} in this conversation, and a delivered order "
            f"accepts only one. Do not submit another; tell the user no "
            f"further return or exchange is possible on this order."
        )
    if tool_name in LOCKABLE_FOLLOWUP_TOOL_NAMES and any(
        name == "modify_pending_order_items"
        and str(args.get("order_id") or "") == order_id
        for name, args in facts.successful_writes
    ):
        return (
            f"VALIDATOR: modify_pending_order_items already ran on order "
            f"{order_id} in this conversation and locked it against further "
            f"changes; address and payment changes must happen BEFORE the "
            f"item modification. Tell the user this change can no longer be "
            f"applied to this order."
        )
    if tool_name not in ITEM_LIST_TOOL_NAMES:
        return None
    order = facts.orders.get(order_id)
    if order is None:
        return (
            f"VALIDATOR: Order {order_id} has not been fetched in this "
            f"conversation, so its items cannot be verified. Fetch the order "
            f"first with get_order_details(order_id='{order_id}'), confirm "
            f"the item ids, then retry."
        )
    order_items: dict[str, dict[str, Any]] = {
        str(item.get("item_id")): item for item in order.get("items") or []
    }
    item_ids = [str(item_id) for item_id in arguments.get("item_ids") or []]
    missing = sorted({item_id for item_id in item_ids if item_id not in order_items})
    if missing:
        return (
            f"VALIDATOR: Item(s) {', '.join(missing)} are not in order "
            f"{order_id} (it contains: {', '.join(sorted(order_items))}). "
            f"Re-check with get_order_details which order actually contains "
            f"each item, then retry with matching order and item ids."
        )
    if tool_name not in REPLACEMENT_TOOL_NAMES:
        return None
    new_item_ids = [str(item_id) for item_id in arguments.get("new_item_ids") or []]
    if len(new_item_ids) != len(item_ids):
        return (
            f"VALIDATOR: item_ids has {len(item_ids)} entries but "
            f"new_item_ids has {len(new_item_ids)}; each new item id must sit "
            f"at the same position as the item it replaces. Align the two "
            f"lists and retry."
        )
    for current_id, new_id in zip(item_ids, new_item_ids):
        current = order_items[current_id]
        product_id = str(current.get("product_id") or "")
        product = facts.products.get(product_id)
        if product is None:
            return (
                f"VALIDATOR: Product {product_id} (for item {current_id}) has "
                f"not been fetched in this conversation, so replacement "
                f"{new_id} cannot be verified. Call "
                f"get_product_details(product_id='{product_id}') first, pick "
                f"an available variant, then retry."
            )
        variant = (product.get("variants") or {}).get(new_id)
        if variant is None:
            return (
                f"VALIDATOR: {new_id} is not a variant of product "
                f"{product_id}, so it cannot replace item {current_id}. "
                f"Choose the new item id from that product's variant list in "
                f"get_product_details and retry."
            )
        if not variant.get("available", False):
            return (
                f"VALIDATOR: Variant {new_id} of product {product_id} is "
                f"marked available=false. Choose an available variant of the "
                f"same product and retry, or tell the user this variant is "
                f"out of stock."
            )
        current_options = {
            str(key): str(value)
            for key, value in (current.get("options") or {}).items()
        }
        for option, raw_value in (variant.get("options") or {}).items():
            new_value = str(raw_value)
            old_value = current_options.get(str(option))
            if old_value is None or old_value.lower() == new_value.lower():
                continue
            if not _mentioned_by_user(new_value, facts.user_text):
                return (
                    f"VALIDATOR: Replacement {new_id} changes '{option}' from "
                    f"'{old_value}' to '{new_value}', but the user has not "
                    f"mentioned '{new_value}'. Ask the user to explicitly "
                    f"confirm this '{option}' change (or choose a variant "
                    f"that keeps '{old_value}'), then retry."
                )
    return None
