import asyncio
from types import SimpleNamespace

from scout.agents import supervisor
from scout.agents.diagnostics import SAFE_TIMEOUT_REPLY
from scout.agents.evidence import record_tool_call
from scout.agents.intent_splitter import split_intents_with_metadata
from scout.api.chat import ChatRequest, ChatResponse
from scout.db.session import SessionLocal
from scout.services.order_service import check_return_eligibility_for_customer, get_order_for_customer


class App:
    scout_specialists = {
        "recommend_agent": object(),
        "inventory_agent": object(),
        "order_agent": object(),
        "external_offer_agent": object(),
        "policy_agent": object(),
    }


PRODUCTS = [
    {"product_id": "P001", "name": "Black Midi Dress", "price": 79.99, "source": "internal"},
    {"product_id": "P003", "name": "Wrap Dress", "price": 68.0, "source": "internal"},
]


async def _fake_tool(calls, tool_name, args, *, agent_name):
    calls.append((agent_name, tool_name, dict(args)))
    result = _tool_result(tool_name, args)
    record_tool_call(
        tool_name=tool_name,
        validated_args=args,
        success=True,
        result=result,
        agent_name=agent_name,
    )
    return result


def _tool_result(tool_name, args):
    if tool_name == "recommend_products":
        query = str(args.get("query", "")).lower()
        if "waterproof" in query:
            return []
        return PRODUCTS
    if tool_name == "stores":
        if args["product_id"] == "P003" and not args.get("store_name"):
            return {
                "found": True,
                "product_id": args["product_id"],
                "requested_store_name": None,
                "requested_store_had_no_stock": False,
                "stores": [
                    {"product_id": "P003", "store_id": "S01", "store_name": "Maple Grove", "quantity": 5, "in_stock": True},
                    {"product_id": "P003", "store_id": "S02", "store_name": "Downtown Minneapolis", "quantity": 5, "in_stock": True},
                    {"product_id": "P003", "store_id": "S03", "store_name": "Ridgedale", "quantity": 5, "in_stock": True},
                ],
            }
        quantity = 2 if args["product_id"] == "P001" else 0
        return {
            "found": True,
            "product_id": args["product_id"],
            "requested_store_name": args.get("store_name"),
            "requested_store_had_no_stock": quantity == 0,
            "stores": [
                {
                    "product_id": args["product_id"],
                    "store_id": "S01",
                    "store_name": args.get("store_name") or "Maple Grove",
                    "quantity": quantity,
                    "in_stock": quantity > 0,
                }
            ],
        }
    if tool_name == "stock":
        return {
            "product_id": args["product_id"],
            "size": args.get("size"),
            "color": args.get("color"),
            "quantity": 0,
            "in_stock": False,
        }
    if tool_name == "retrieve_policy_chunks":
        if "third-party" in str(args.get("query", "")).lower():
            return [
                {
                    "statement": "Scout return policy applies to purchases from Scout.",
                    "policy_name": "Return Policy",
                    "source_document": "returns.md",
                    "source_section": "scope",
                }
            ]
        return [
            {
                "statement": "Opened or worn items are not eligible for return unless defective.",
                "policy_name": "Return Policy",
                "source_document": "returns.md",
                "source_section": "returns",
            }
        ]
    if tool_name == "orders":
        return {"order_id": args["order_id"], "status": "shipped", "found": True}
    if tool_name == "shipment_status":
        return {
            "order_id": args["order_id"],
            "found": True,
            "authorized": True,
            "shipped": True,
            "carrier": "UPS",
            "tracking_number": "1Z999AA10123456784",
            "status": "in_transit",
            "estimated_delivery_date": "2026-08-16",
        }
    if tool_name == "return_eligibility":
        return {"order_id": args["order_id"], "likely_eligible": True, "reason": "Within the return window."}
    if tool_name == "search_external_offers":
        return [
            {
                "external_product_id": "EX001",
                "name": "Waterproof Hiking Shoe",
                "vendor_name": "Trail Vendor",
                "price": 59.99,
                "source": "external",
                "click_url": "/affiliate/click/EX001",
            }
        ]
    raise AssertionError(f"unexpected tool {tool_name}")


def _run_with_fake_tools(monkeypatch, message, conversation_context=None):
    calls = []
    graph_markers = []
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", lambda tool, args, *, agent_name: _fake_tool(calls, tool, args, agent_name=agent_name))
    monkeypatch.setattr(supervisor, "_record_multi_intent_supervisor_path", lambda plan: graph_markers.append([s.intent for s in plan.subgoals]))
    reply, history, products = asyncio.run(supervisor.ask(App(), [], message, conversation_context=conversation_context))
    return SimpleNamespace(reply=reply, history=history, products=products, calls=calls, graph_markers=graph_markers)


def _run_with_tool_overrides(monkeypatch, message, overrides):
    calls = []
    graph_markers = []

    async def fake_tool(tool_name, args, *, agent_name):
        calls.append((agent_name, tool_name, dict(args)))
        result = overrides.get(tool_name, _tool_result)(tool_name, args)
        record_tool_call(
            tool_name=tool_name,
            validated_args=args,
            success=True,
            result=result,
            agent_name=agent_name,
        )
        return result

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    monkeypatch.setattr(supervisor, "_record_multi_intent_supervisor_path", lambda plan: graph_markers.append([s.intent for s in plan.subgoals]))
    reply, history, products = asyncio.run(supervisor.ask(App(), [], message))
    return SimpleNamespace(reply=reply, history=history, products=products, calls=calls, graph_markers=graph_markers)


def test_mi_01_recommendation_transfers_product_ids_to_store_inventory(monkeypatch):
    result = _run_with_fake_tools(
        monkeypatch,
        "Recommend a dress under $80 and check which one is available at Maple Grove.",
    )

    assert result.graph_markers == [["product_recommendation", "store_availability"]]
    assert [call[1] for call in result.calls] == ["recommend_products", "stores", "stores"]
    assert result.calls[1][2]["product_id"] == "P001"
    assert result.calls[1][2]["store_name"] == "Maple Grove"
    assert "I found 2 dresses in our catalog" in result.reply
    assert "Black Midi Dress for $79.99" in result.reply
    assert "Maple Grove store with 2 units" in result.reply
    assert [product["product_id"] for product in result.products] == ["P001", "P003"]


def test_mi_02_recommendation_and_policy_complete_without_splitter_timeout(monkeypatch):
    result = _run_with_fake_tools(monkeypatch, "Recommend hiking shoes under $100 and explain the return policy.")

    assert result.graph_markers == [["product_recommendation", "policy_question"]]
    assert [call[1] for call in result.calls] == ["recommend_products", "retrieve_policy_chunks"]
    assert "Opened or worn items usually aren’t eligible for a return unless they’re defective" in result.reply


def test_mi_03_constraints_survive_recommendation_inventory_policy_handoff(monkeypatch):
    result = _run_with_fake_tools(
        monkeypatch,
        "Recommend a black dress under $80, check whether it is available in medium at Maple Grove, and explain whether I can return it after opening it.",
    )

    assert result.graph_markers == [["product_recommendation", "store_availability", "policy_question"]]
    stock_calls = [call for call in result.calls if call[1] == "stock"]
    store_calls = [call for call in result.calls if call[1] == "stores"]
    assert stock_calls[0][2] == {"product_id": "P001", "size": "M", "color": "black"}
    assert store_calls[0][2] == {"product_id": "P001", "store_name": "Maple Grove"}
    assert "Maple Grove has other inventory for that item, but the black, medium option is currently out of stock" in result.reply
    assert "Opened or worn items usually aren’t eligible for a return unless they’re defective" in result.reply


def test_mi_03_short_demo_prompt_is_not_swallowed_by_follow_up_context(monkeypatch):
    context = {
        "active_product_id": "P002",
        "active_product_name": "Floral Sundress",
        "active_selected_products": [{"product_id": "P002", "name": "Floral Sundress"}],
        "requested_size": "M",
        "requested_color": "black",
        "requested_store": "Maple Grove",
    }

    result = _run_with_fake_tools(
        monkeypatch,
        "Recommend a black dress under $80, check Maple Grove medium availability, and explain opened-item returns.",
        conversation_context=context,
    )

    assert result.graph_markers == [["product_recommendation", "store_availability", "policy_question"]]
    assert [call[1] for call in result.calls] == [
        "recommend_products",
        "stock",
        "stores",
        "stock",
        "stores",
        "retrieve_policy_chunks",
    ]
    assert result.calls[1][2] == {"product_id": "P001", "size": "M", "color": "black"}
    assert result.calls[2][2] == {"product_id": "P001", "store_name": "Maple Grove"}
    assert "Floral Sundress" not in result.reply
    assert "Maple Grove has other inventory for that item, but the black, medium option is currently out of stock" in result.reply
    assert "Opened or worn items usually aren’t eligible for a return unless they’re defective" in result.reply


def test_mi_04_order_status_and_return_eligibility_are_read_only(monkeypatch):
    result = _run_with_fake_tools(monkeypatch, "Where is order O1001, and is it eligible for return?")

    assert result.graph_markers == [["order_status", "return_eligibility"]]
    assert [call[1] for call in result.calls] == ["orders", "return_eligibility"]
    # Behavior check, not exact wording - the routing/tool-call
    # assertions above are this test's real purpose.
    assert "O1001" in result.reply
    assert "shipped" in result.reply.lower()
    assert "eligible" in result.reply.lower()
    assert not any(call[1] in {"refund", "cancel", "checkout", "payment"} for call in result.calls)


def test_order_status_question_uses_order_agent_only(monkeypatch):
    # "Where is O1001?" specifically asks about location/shipping, so as
    # of Phase 3 (shipment tracking), this correctly routes to the
    # richer shipment_status tool instead of plain order status - see
    # tool_first.py's shipping_terms check.
    result = _run_with_fake_tools(monkeypatch, "Where is O1001?")

    assert result.graph_markers == []
    assert [call[0] for call in result.calls] == ["order_agent"]
    assert [call[1] for call in result.calls] == ["shipment_status"]


def test_plain_order_status_question_still_uses_orders_tool(monkeypatch):
    # A genuinely plain status question (no shipping-specific words)
    # still correctly uses the simpler orders tool, not shipment_status.
    result = _run_with_fake_tools(monkeypatch, "What is the status of O1001?")

    assert result.graph_markers == []
    assert [call[0] for call in result.calls] == ["order_agent"]
    assert [call[1] for call in result.calls] == ["orders"]
    assert "Order O1001 has shipped" in result.reply


def test_simple_return_policy_question_uses_policy_agent_only(monkeypatch):
    app = App()
    split = split_intents_with_metadata(object(), "What is your return policy?")

    assert split.execution_plan is None
    assert split.structured_intent.request_type == "policy_question"
    assert supervisor._direct_route_agent(app, split.structured_intent, split.sub_intents) == "policy_agent"


def test_order_specific_return_reason_uses_order_then_policy(monkeypatch):
    result = _run_with_fake_tools(monkeypatch, "Can I return order O1001, and why?")

    assert result.graph_markers == [["order_status", "return_eligibility", "policy_question"]]
    assert [call[0] for call in result.calls] == ["order_agent", "order_agent", "policy_agent"]
    assert [call[1] for call in result.calls] == ["orders", "return_eligibility", "retrieve_policy_chunks"]
    assert result.calls[2][2]["query"] == "Explain the Scout return policy rule for order return eligibility."
    assert "Order O1001 has shipped" in result.reply
    assert "eligible" in result.reply.lower()
    assert "Opened or worn items usually aren’t eligible" in result.reply


def test_unauthorized_order_stops_before_policy_explanation(monkeypatch):
    def unauthorized_order(tool_name, args):
        return {
            "authorized": False,
            "error_code": "order_access_denied",
            "order_id": args["order_id"],
        }

    result = _run_with_tool_overrides(
        monkeypatch,
        "Can I return order O1001, and why?",
        {"orders": unauthorized_order},
    )

    assert result.graph_markers == [["order_status", "return_eligibility", "policy_question"]]
    assert [call[1] for call in result.calls] == ["orders"]
    assert "shipped" not in result.reply.lower()
    assert "eligible for return" not in result.reply.lower()
    assert "Opened or worn items" not in result.reply


def test_missing_policy_evidence_does_not_invent_order_policy_reason(monkeypatch):
    def empty_policy(tool_name, args):
        return []

    result = _run_with_tool_overrides(
        monkeypatch,
        "Can I return order O1001, and why?",
        {"retrieve_policy_chunks": empty_policy},
    )

    assert [call[1] for call in result.calls] == ["orders", "return_eligibility", "retrieve_policy_chunks"]
    assert "Order O1001 has shipped" in result.reply
    assert "eligible" in result.reply.lower()
    assert "Opened or worn items" not in result.reply
    assert "30 days" not in result.reply


def test_order_specific_worn_item_policy_uses_real_o1001_item_and_targeted_policy(monkeypatch):
    policy_queries = []

    def real_owned_order(tool_name, args):
        session = SessionLocal()
        try:
            if tool_name == "orders":
                return get_order_for_customer(session, args["order_id"], authenticated_customer_id="C001")
            if tool_name == "return_eligibility":
                return check_return_eligibility_for_customer(session, args["order_id"], authenticated_customer_id="C001")
        finally:
            session.close()
        raise AssertionError(f"unexpected order tool {tool_name}")

    def policy_result(tool_name, args):
        query = args["query"]
        policy_queries.append(query)
        if "opened used worn" in query:
            return [
                {
                    "statement": (
                        "Items may be returned within 30 days of delivery if unworn, unwashed, "
                        "and original tags are attached. Opened or worn items are not eligible "
                        "for return unless defective."
                    ),
                    "policy_name": "Returns",
                    "source_document": "returns.md",
                    "source_section": "returns",
                }
            ]
        return [
            {
                "statement": "Items may be returned within 30 days of delivery.",
                "policy_name": "Returns",
                "source_document": "returns.md",
                "source_section": "returns",
            }
        ]

    result = _run_with_tool_overrides(
        monkeypatch,
        "Can I return order O1001 if the Running Shoes are worn, and why?",
        {
            "orders": real_owned_order,
            "return_eligibility": real_owned_order,
            "retrieve_policy_chunks": policy_result,
        },
    )

    assert result.graph_markers == [["order_status", "return_eligibility", "policy_question"]]
    assert [call[1] for call in result.calls] == [
        "orders",
        "return_eligibility",
        "retrieve_policy_chunks",
        "retrieve_policy_chunks",
    ]
    assert result.calls[0][2] == {"order_id": "O1001"}
    assert result.calls[1][2] == {"order_id": "O1001"}
    assert len(policy_queries) == 2
    assert policy_queries[0] == "Explain the Scout return policy rule for order return eligibility."
    assert policy_queries[1] == "Scout return policy opened used worn items defective original tags"
    assert all("Running Shoes" not in query and "P005" not in query and "O1001" not in query for query in policy_queries)
    assert "Order O1001 has shipped" in result.reply
    assert "eligible" in result.reply.lower()
    assert "Opened or worn items usually aren’t eligible" in result.reply


def test_o1001_seeded_order_item_is_running_shoes():
    session = SessionLocal()
    try:
        order = get_order_for_customer(session, "O1001", authenticated_customer_id="C001")
    finally:
        session.close()

    assert order["authorized"] is True
    assert order["items"] == [
        {
            "product_id": "P005",
            "name": "Running Shoes",
            "quantity": 1,
            "price_at_purchase": 64.99,
        }
    ]


def test_mi_05_external_fallback_after_internal_insufficiency(monkeypatch):
    result = _run_with_fake_tools(
        monkeypatch,
        "Find waterproof hiking shoes under $70. If Scout has none, show an outside option and explain its return-policy limitation.",
    )

    assert result.graph_markers == [["product_recommendation", "external_offer", "policy_question"]]
    assert [call[1] for call in result.calls] == [
        "recommend_products",
        "search_external_offers",
    ]
    assert result.products == [
        {
            "external_product_id": "EX001",
            "name": "Waterproof Hiking Shoe",
            "vendor_name": "Trail Vendor",
            "source": "external",
            "price": 59.99,
            "click_url": "/affiliate/click/EX001",
        }
    ]
    # Behavior check: the real external product/vendor/price appear,
    # clearly framed as another retailer's option - exact phrasing may
    # evolve independently.
    assert "Waterproof Hiking Shoe" in result.reply
    assert "Trail Vendor" in result.reply
    assert "$59.99" in result.reply
    assert "retailer" in result.reply.lower()
    assert "return policy" in result.reply.lower() and "won’t apply" in result.reply.lower()
    assert "return-policy" in result.reply.lower() or "return policy" in result.reply.lower()
    assert "Scout return policy applies" not in result.reply


def test_compound_request_cannot_enter_single_intent_direct_routing():
    app = App()
    split = split_intents_with_metadata(
        object(),
        "Recommend a dress under $80 and check which one is available at Maple Grove.",
    )

    assert split.execution_plan is not None
    assert supervisor._direct_route_agent(app, split.structured_intent, split.sub_intents) is None


def test_comparison_plan_inserts_ranking_before_store_handoff():
    split = split_intents_with_metadata(
        object(),
        "Compare these three dresses, pick the best under $80, and check which stores have it.",
    )

    assert split.execution_plan is not None
    assert [subgoal.intent for subgoal in split.execution_plan.subgoals] == [
        "product_recommendation",
        "product_comparison",
        "store_availability",
    ]
    assert split.execution_plan.budget_max == 80
    assert split.execution_plan.product_type == "dresses"


def test_comparison_selects_best_product_before_store_lookup(monkeypatch):
    result = _run_with_fake_tools(
        monkeypatch,
        "Compare these three dresses, pick the best under $80, and check which stores have it.",
    )

    assert result.graph_markers == [["product_recommendation", "product_comparison", "store_availability"]]
    assert [call[1] for call in result.calls] == ["recommend_products", "stores"]
    assert result.calls[1][2] == {"product_id": "P003"}
    assert "Of the 2, I’d pick Wrap Dress. It’s under $80 at $68.00" in result.reply
    assert "The Wrap Dress is available at Maple Grove, Downtown Minneapolis, and Ridgedale, with 5 units at each store." in result.reply


def test_contextual_comparison_reuses_prior_product_cards(monkeypatch):
    context = {
        "active_selected_products": [
            {
                "product_id": "P003",
                "name": "Wrap Dress",
                "price": 68.0,
                "rating": 4.4,
                "source": "internal",
                "promotion": {"discounted_price": 57.8},
                "image_url": "/static/products/P003.jpg",
            },
            {
                "product_id": "P004",
                "name": "Slip Dress",
                "price": 62.5,
                "rating": 3.9,
                "source": "internal",
                "promotion": {"discounted_price": 53.12},
            },
            {
                "product_id": "P001",
                "name": "Black Midi Dress",
                "price": 79.99,
                "rating": 4.3,
                "source": "internal",
                "promotion": {"discounted_price": 67.99},
            },
        ],
    }

    result = _run_with_fake_tools(
        monkeypatch,
        "Compare these three dresses, pick the best under $80, and check which stores have it.",
        conversation_context=context,
    )

    assert result.graph_markers == [["product_recommendation", "product_comparison", "store_availability"]]
    assert [call[1] for call in result.calls] == ["stores"]
    assert result.calls[0][2] == {"product_id": "P003"}
    assert "Of the 3, I’d pick Wrap Dress. It’s under $80 at $68.00, has the highest rating at 4.4 and its sale price is $57.80." in result.reply
    assert "The Wrap Dress is available at Maple Grove, Downtown Minneapolis, and Ridgedale, with 5 units at each store." in result.reply
    assert result.products == [
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


def test_comparison_only_uses_verified_products_without_extra_tool_calls(monkeypatch):
    result = _run_with_fake_tools(
        monkeypatch,
        "Compare these dresses and pick the best under $80.",
    )

    assert result.graph_markers == [["product_recommendation", "product_comparison"]]
    assert [call[1] for call in result.calls] == ["recommend_products"]
    assert "Of the 2, I’d pick Wrap Dress. It’s under $80 at $68.00" in result.reply
    assert [product["product_id"] for product in result.products] == ["P001", "P003"]


def test_single_intent_direct_routing_remains_unchanged():
    app = App()
    cases = {
        "Recommend a dress under $80.": "recommend_agent",
        "Is the black dress available at Maple Grove?": "inventory_agent",
        "What is your return policy?": "policy_agent",
        "Where is order O1001?": "order_agent",
    }
    for message, expected in cases.items():
        split = split_intents_with_metadata(object(), message)
        assert split.execution_plan is None
        assert supervisor._direct_route_agent(app, split.structured_intent, split.sub_intents) == expected


def test_streaming_non_streaming_and_api_schemas_remain_compatible(monkeypatch):
    message = "Recommend a dress under $80 and check which one is available at Maple Grove."
    calls = []
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", lambda tool, args, *, agent_name: _fake_tool(calls, tool, args, agent_name=agent_name))

    non_reply, _history, non_products = asyncio.run(supervisor.ask(App(), [], message))
    events = asyncio.run(_collect(supervisor.ask_streaming(App(), [], message)))

    assert events[-1] == ("result", (non_reply, non_products))
    assert set(ChatRequest.model_fields) == {"message", "session_id"}
    assert set(ChatResponse.model_fields) == {"session_id", "reply", "products"}


def test_provider_timeout_still_returns_safe_response(monkeypatch):
    monkeypatch.setattr(supervisor.settings, "CHAT_REQUEST_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())

    async def slow_tool(*args, **kwargs):
        await asyncio.sleep(1)

    monkeypatch.setattr(supervisor, "_execute_read_only_tool", slow_tool)

    reply, _history, products = asyncio.run(
        supervisor.ask(App(), [], "Recommend a dress under $80 and check which one is available at Maple Grove.")
    )

    assert reply == SAFE_TIMEOUT_REPLY
    assert products == []


async def _collect(async_iterable):
    return [item async for item in async_iterable]
