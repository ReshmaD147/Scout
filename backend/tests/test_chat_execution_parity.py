import asyncio
import json
from copy import deepcopy
from types import SimpleNamespace

from scout.agents import supervisor
from scout.agents.diagnostics import SAFE_TIMEOUT_REPLY
from scout.agents.evidence import record_tool_call


def ai(name, content):
    return SimpleNamespace(name=name, content=content, type="ai", tool_calls=None)


def tool(name, content):
    return SimpleNamespace(name=name, content=content, type="tool")


PRODUCTS = [
    {
        "product_id": "P003",
        "name": "Wrap Dress",
        "source": "internal",
        "price": 68.0,
        "rating": 4.4,
        "promotion": {"discounted_price": 57.8},
        "image_url": "/static/products/P003.jpg",
    },
    {
        "product_id": "P004",
        "name": "Slip Dress",
        "source": "internal",
        "price": 62.5,
        "rating": 3.9,
        "promotion": {"discounted_price": 53.12},
        "image_url": "/static/products/P004.jpg",
    },
    {
        "product_id": "P001",
        "name": "Black Midi Dress",
        "source": "internal",
        "price": 79.99,
        "rating": 4.3,
        "promotion": {"discounted_price": 67.99},
        "image_url": "/static/products/P001.jpg",
    },
]


class FakeAgent:
    def __init__(self, agent_name, tool_name=None, result=None, reply="Supported reply."):
        self.agent_name = agent_name
        self.tool_name = tool_name
        self.result = result
        self.reply = reply

    async def ainvoke(self, payload, config):
        messages = [*payload["messages"]]
        if self.tool_name:
            record_tool_call(
                tool_name=self.tool_name,
                validated_args={},
                success=True,
                result=self.result,
                agent_name=self.agent_name,
            )
            messages.append(tool(self.tool_name, json.dumps(self.result)))
        messages.append(ai(self.agent_name, self.reply))
        return {"messages": messages}


class FakeInventoryAgent:
    async def ainvoke(self, payload, config):
        messages = [*payload["messages"]]
        latest = messages[-1] if messages else {}
        text = str(latest.get("content") if isinstance(latest, dict) else getattr(latest, "content", "")).lower()
        if "maple grove" in text:
            tool_name = "stores"
            args = {"product_id": "P001", "store_name": "Maple Grove"}
        elif "deliver" in text or "delivery" in text or "ship" in text:
            tool_name = "fulfillment_options"
            args = {"product_id": "P001", "size": "M", "color": "black"}
        else:
            tool_name = "stock"
            args = {"product_id": "P001", "size": "M", "color": "black"}
        result = _tool_result(tool_name, args)
        record_tool_call(
            tool_name=tool_name,
            validated_args=args,
            success=True,
            result=result,
            agent_name="inventory_agent",
        )
        messages.append(tool(tool_name, json.dumps(result)))
        messages.append(ai("inventory_agent", "Inventory result."))
        return {"messages": messages}


class ProviderFailingAgent:
    async def ainvoke(self, payload, config):
        class ReadTimeout(Exception):
            __module__ = "httpx"

        raise ReadTimeout("provider details must not leak")


class App:
    def __init__(self, *, provider_fails=False):
        agent = ProviderFailingAgent() if provider_fails else None
        self.scout_specialists = {
            "recommend_agent": agent or FakeAgent(
                "recommend_agent",
                "recommend_products",
                PRODUCTS,
                "I found 3 Scout dresses.",
            ),
            "inventory_agent": agent or FakeInventoryAgent(),
            "order_agent": agent or FakeAgent(
                "order_agent",
                "orders",
                _tool_result("orders", {"order_id": "O1001"}),
                "Order O1001 has shipped.",
            ),
            "external_offer_agent": agent or FakeAgent("external_offer_agent"),
            "policy_agent": agent or FakeAgent(
                "policy_agent",
                "retrieve_policy_chunks",
                _tool_result("retrieve_policy_chunks", {"query": "return policy"}),
                "Opened or worn items are not eligible for return unless defective.",
            ),
        }

    async def ainvoke(self, payload, config):
        return {"messages": [*payload["messages"], ai("policy_agent", "Graph reply.")]}


def _tool_result(tool_name, args):
    if tool_name == "recommend_products":
        query = str(args.get("query", "")).lower()
        if "red cocktail" in query:
            return []
        return PRODUCTS
    if tool_name == "stock":
        size = str(args.get("size") or "").upper()
        color = str(args.get("color") or "").lower()
        quantity = 0 if args.get("product_id") == "P001" and size == "M" and color == "black" else 3
        return {
            "product_id": args["product_id"],
            "size": args.get("size"),
            "color": args.get("color"),
            "quantity": quantity,
            "in_stock": quantity > 0,
        }
    if tool_name == "stores":
        if not args.get("store_name"):
            return {
                "found": True,
                "product_id": args["product_id"],
                "requested_store_name": None,
                "requested_store_had_no_stock": False,
                "stores": [
                    {
                        "product_id": args["product_id"],
                        "store_id": "S01",
                        "store_name": "Maple Grove",
                        "quantity": 5,
                        "in_stock": True,
                    },
                    {
                        "product_id": args["product_id"],
                        "store_id": "S02",
                        "store_name": "Downtown Minneapolis",
                        "quantity": 5,
                        "in_stock": True,
                    },
                ],
            }
        return {
            "found": True,
            "product_id": args["product_id"],
            "requested_store_name": args.get("store_name"),
            "requested_store_had_no_stock": False,
            "stores": [
                {
                    "product_id": args["product_id"],
                    "store_id": "S01",
                    "store_name": args.get("store_name"),
                    "quantity": 2,
                    "in_stock": True,
                }
            ],
        }
    if tool_name == "fulfillment_options":
        return {
            "product_id": args["product_id"],
            "size": args.get("size"),
            "color": args.get("color"),
            "delivery": {"available": True},
            "pickup": {"available": False, "locations": []},
        }
    if tool_name == "alternatives":
        if args.get("product_id") == "NO_MATCH":
            items = []
        else:
            items = [
                {
                    "product_id": "P003",
                    "name": "Wrap Dress",
                    "source": "internal",
                    "price": 68.0,
                    "rating": 4.4,
                    "promotion": {"discounted_price": 57.8},
                    "image_url": "/static/products/P003.jpg",
                }
            ]
        return {
            "product_id": args["product_id"],
            "items": items,
            "match_count": len(items),
            "candidate_count": len(items),
            "filters": {
                "size": args.get("size"),
                "color": args.get("color"),
                "max_price": args.get("max_price"),
                "category": args.get("category"),
            },
        }
    if tool_name == "orders":
        return {
            "authorized": True,
            "found": True,
            "order_id": args["order_id"],
            "customer_id": "C001",
            "status": "shipped",
            "items": [{"product_id": "P005", "name": "Running Shoes", "quantity": 1}],
        }
    if tool_name == "shipment_status":
        return {
            "found": True,
            "authorized": True,
            "order_id": args["order_id"],
            "shipped": True,
            "carrier": "UPS",
            "tracking_number": "1Z999AA10123456784",
            "status": "in_transit",
            "estimated_delivery_date": "2026-08-16",
        }
    if tool_name == "return_eligibility":
        return {
            "authorized": True,
            "found": True,
            "order_id": args["order_id"],
            "status": "shipped",
            "likely_eligible": True,
            "reason": "Within the return window.",
        }
    if tool_name == "retrieve_policy_chunks":
        return [
            {
                "statement": "Opened or worn items are not eligible for return unless defective.",
                "policy_name": "Return Policy",
                "source_document": "returns.md",
                "source_section": "returns",
            }
        ]
    raise AssertionError(f"unexpected tool {tool_name}")


async def _fake_tool(tool_name, args, *, agent_name):
    result = _tool_result(tool_name, args)
    record_tool_call(
        tool_name=tool_name,
        validated_args=args,
        success=True,
        result=result,
        agent_name=agent_name,
    )
    return result


async def _collect_stream(app, history, message, conversation_context=None):
    events = []
    final = None
    async for kind, payload in supervisor.ask_streaming(
        app,
        history,
        message,
        conversation_context=conversation_context,
    ):
        if kind == "result":
            final = payload
        else:
            events.append((kind, payload))
    assert final is not None
    return final, events


def _assert_ask_streaming_parity(monkeypatch, message, *, conversation_context=None, provider_fails=False):
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", _fake_tool)
    non_context = deepcopy(conversation_context) if conversation_context is not None else None
    stream_context = deepcopy(conversation_context) if conversation_context is not None else None

    non_reply, _non_history, non_products = asyncio.run(
        supervisor.ask(
            App(provider_fails=provider_fails),
            [],
            message,
            conversation_context=non_context,
        )
    )
    stream_result, progress_events = asyncio.run(
        _collect_stream(
            App(provider_fails=provider_fails),
            [],
            message,
            conversation_context=stream_context,
        )
    )

    assert stream_result == (non_reply, non_products)
    return SimpleNamespace(reply=non_reply, products=non_products, progress_events=progress_events)


def _variant_context(product_id="P001"):
    return {
        "active_product_id": product_id,
        "active_product_name": "Black Midi Dress",
        "requested_color": "black",
        "requested_size": "M",
        "active_category": "dresses",
        "budget_max": 80,
        # Represents the genuine out-of-stock scenario this fixture is
        # meant to simulate - required for recovery-action follow-ups
        # (delivery/pickup/store questions) to be recognized correctly,
        # per the context-gating fix.
        "last_out_of_stock_product_id": product_id,
        "active_selected_products": [
            {
                "product_id": product_id,
                "name": "Black Midi Dress",
                "source": "internal",
                "price": 79.99,
                "rating": 4.3,
            }
        ],
    }


def test_streaming_recommendation_selection_requires_size_before_cart_offer(monkeypatch):
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    context = {
        "active_selected_products": [
            {
                "product_id": "P003",
                "name": "Wrap Dress",
                "recommendation_id": "rec_wrap",
                "recommendation_session_id": "sess_wrap",
            },
            {
                "product_id": "P004",
                "name": "Slip Dress",
                "recommendation_id": "rec_slip",
                "recommendation_session_id": "sess_slip",
            },
        ],
    }

    (reply, products), progress_events = asyncio.run(
        _collect_stream(
            App(),
            [],
            "I like the second one",
            conversation_context=context,
        )
    )

    # Streaming must enforce the same variant-safety invariant as the
    # non-streaming path: a product with sizes cannot reach a cart offer
    # until the customer chooses a size.
    assert reply == "Nice pick — what size would you like?"
    assert products == []
    assert progress_events == []

    assert context.get("pending_cart_offer") is None
    assert context["active_product_id"] == "P004"
    assert context["active_product_name"] == "Slip Dress"


def test_product_recommendation_final_result_matches_streaming(monkeypatch):
    result = _assert_ask_streaming_parity(monkeypatch, "Recommend a dress under $80")

    assert "dresses in our catalog" in result.reply
    assert [product["product_id"] for product in result.products] == ["P003", "P004", "P001"]
    assert result.progress_events


def test_exact_variant_inventory_final_result_matches_streaming(monkeypatch):
    result = _assert_ask_streaming_parity(monkeypatch, "Is the black midi dress in a medium?")

    assert "out of stock" in result.reply
    assert "black, medium" in result.reply
    assert result.products == []


def test_store_availability_final_result_matches_streaming(monkeypatch):
    result = _assert_ask_streaming_parity(monkeypatch, "Is the black midi dress available at Maple Grove?")

    assert "Maple Grove" in result.reply
    assert "2 available" in result.reply
    assert result.products == []


def test_delivery_follow_up_final_result_matches_streaming(monkeypatch):
    result = _assert_ask_streaming_parity(
        monkeypatch,
        "Can it be delivered?",
        conversation_context=_variant_context(),
    )

    assert "Black Midi Dress" in result.reply
    assert "delivery" in result.reply.lower()
    assert result.products == []


def test_similar_products_success_final_result_matches_streaming(monkeypatch):
    result = _assert_ask_streaming_parity(
        monkeypatch,
        "Show me something similar",
        conversation_context=_variant_context(),
    )

    assert "Wrap Dress" in result.reply
    assert [product["product_id"] for product in result.products] == ["P003"]


def test_similar_products_no_match_special_reply_matches_streaming(monkeypatch):
    context = _variant_context(product_id="NO_MATCH")

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", _fake_tool)

    non_reply, _history, non_products = asyncio.run(
        supervisor.ask(App(), [], "Anything else like this?", conversation_context=deepcopy(context))
    )
    stream_result, _progress_events = asyncio.run(
        _collect_stream(App(), [], "Anything else like this?", conversation_context=deepcopy(context))
    )

    assert stream_result == (non_reply, non_products)
    assert non_reply == "I don’t see another close alternative to Black Midi Dress right now."
    assert non_products == []


def test_authenticated_order_status_final_result_matches_streaming(monkeypatch):
    result = _assert_ask_streaming_parity(
        monkeypatch,
        "Where is order O1001?",
        conversation_context={"authenticated_customer_id": "C001"},
    )

    assert "O1001" in result.reply
    assert "in transit" in result.reply.lower()
    assert "ups" in result.reply.lower()
    assert result.products == []


def test_return_eligibility_final_result_matches_streaming(monkeypatch):
    result = _assert_ask_streaming_parity(
        monkeypatch,
        "Can I return O1001?",
        conversation_context={"authenticated_customer_id": "C001"},
    )

    assert "O1001" in result.reply
    assert "eligible" in result.reply.lower()
    assert result.products == []


def test_general_policy_final_result_matches_streaming(monkeypatch):
    result = _assert_ask_streaming_parity(monkeypatch, "What is your return policy?")

    assert "Opened or worn items" in result.reply or "aren’t eligible" in result.reply
    assert result.products == []


def test_order_policy_multi_intent_final_result_matches_streaming(monkeypatch):
    result = _assert_ask_streaming_parity(
        monkeypatch,
        "Can I return order O1001, and why?",
        conversation_context={"authenticated_customer_id": "C001"},
    )

    assert "O1001" in result.reply
    assert "eligible" in result.reply.lower()
    assert "Opened or worn items" in result.reply or "aren’t eligible" in result.reply
    assert result.products == []


def test_provider_timeout_safe_failure_final_result_matches_streaming(monkeypatch):
    # Deliberately an inventory question, not a recommendation - a
    # clear recommendation request now correctly bypasses the model
    # entirely via a deterministic tool-first path (see tool_first.py),
    # so it would no longer exercise this specific failure mode. This
    # query is unaffected by that change and still correctly routes to
    # the (here, deliberately failing) specialist agent.
    result = _assert_ask_streaming_parity(
        monkeypatch,
        "Is the black midi dress in a medium?",
        provider_fails=True,
    )

    assert result.reply == SAFE_TIMEOUT_REPLY
    assert result.products == []
