import asyncio
from types import SimpleNamespace

from scout.agents import supervisor
from scout.agents.claims import ClaimType
from scout.agents.evidence import clear_evidence_context, get_evidence_entries, record_tool_call, start_evidence_context
from scout.agents.intent_splitter import StructuredIntent
from scout.api.chat import ChatRequest, ChatResponse


def claim(claim_type, subject_id, field, value, claim_id):
    return SimpleNamespace(
        claim_id=claim_id,
        claim_type=claim_type,
        subject_id=subject_id,
        field=field,
        value=value,
        evidence_ids=["ev1"],
        source_agent="inventory_agent",
    )


def finalized(reply="Verified reply.", products=None, claims=None, approved=None, verified=True):
    claims = claims or []
    return supervisor.FinalizedResponse(
        reply=reply,
        products=products or [],
        proposed_claims=claims,
        verification_result=SimpleNamespace(
            verified=verified,
            approved_claim_ids=approved if approved is not None else [item.claim_id for item in claims],
            rejected_claims=[],
            correction_agent=None,
        ),
    )


def user_payload_text(agent):
    for message in agent.payloads[0]["messages"]:
        if isinstance(message, dict) and message.get("role") == "user":
            return message["content"]
    return ""


class CapturingAgent:
    def __init__(self, name):
        self.name = name
        self.calls = 0
        self.payloads = []

    async def ainvoke(self, payload, config):
        self.calls += 1
        self.payloads.append(payload)
        return {"messages": [SimpleNamespace(name=self.name, content="Supported reply.", type="ai", tool_calls=None)]}


class App:
    def __init__(self):
        self.graph_calls = 0
        self.scout_specialists = {
            "recommend_agent": CapturingAgent("recommend_agent"),
            "inventory_agent": CapturingAgent("inventory_agent"),
            "order_agent": CapturingAgent("order_agent"),
            "external_offer_agent": CapturingAgent("external_offer_agent"),
            "policy_agent": CapturingAgent("policy_agent"),
        }

    async def ainvoke(self, payload, config):
        self.graph_calls += 1
        return {"messages": [SimpleNamespace(name="supervisor", content="Graph reply.", type="ai", tool_calls=None)]}


def test_verified_selected_product_becomes_active_context(monkeypatch):
    app = App()
    context = {}
    product = {
        "product_id": "P001",
        "name": "Black Midi Dress",
        "price": 79.99,
        "source": "internal",
        "recommendation_id": "rec_context",
        "recommendation_session_id": "sess_context",
    }

    async def fake_tool(tool_name, args, *, agent_name):
        return [product]

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(products=[product]))

    asyncio.run(supervisor.ask(app, [], "Recommend a dress under $80.", conversation_context=context))

    assert context["active_selected_products"] == [{
        "product_id": "P001",
        "name": "Black Midi Dress",
        "source": "internal",
        "recommendation_id": "rec_context",
        "recommendation_session_id": "sess_context",
        "price": 79.99,
    }]
    assert context["active_product_id"] == "P001"
    assert context["active_product_name"] == "Black Midi Dress"
    assert context["requested_budget_max"] == 80


def test_followup_available_medium_resolves_active_product_and_bypasses_supervisor(monkeypatch):
    app = App()
    context = {
        "active_product_id": "P001",
        "active_product_name": "Black Midi Dress",
        "active_selected_products": [{"product_id": "P001", "name": "Black Midi Dress"}],
    }
    captured = {}

    async def fake_tool(tool_name, args, *, agent_name):
        captured.update(tool_name=tool_name, args=args, agent_name=agent_name)

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(reply="Inventory verified."))

    reply, _history, _products = asyncio.run(
        supervisor.ask(app, [], "Is it available in medium?", conversation_context=context)
    )

    assert reply == "Inventory verified."
    assert app.graph_calls == 0
    assert app.scout_specialists["inventory_agent"].calls == 0
    assert captured == {"tool_name": "stock", "args": {"product_id": "P001", "size": "M"}, "agent_name": "inventory_agent"}


def test_resolved_followup_pickup_preserves_product_size_and_store(monkeypatch):
    app = App()
    context = {
        "active_product_id": "P001",
        "active_product_name": "Black Midi Dress",
        "active_selected_products": [{"product_id": "P001", "name": "Black Midi Dress"}],
        "requested_size": "M",
    }
    captured = {}

    async def fake_tool(tool_name, args, *, agent_name):
        captured.update(tool_name=tool_name, args=args, agent_name=agent_name)

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(reply="Store verified."))

    asyncio.run(
        supervisor.ask(app, [], "Can I pick it up at Maple Grove?", conversation_context=context)
    )

    assert captured == {"tool_name": "stores", "args": {"product_id": "P001", "store_name": "Maple Grove", "size": "M"}, "agent_name": "inventory_agent"}
    assert context["requested_store"] == "Maple Grove"


def test_nearby_store_followup_without_location_asks_for_location(monkeypatch):
    app = App()
    context = {
        "active_product_id": "P001",
        "active_product_name": "Black Midi Dress",
        "active_selected_products": [{"product_id": "P001", "name": "Black Midi Dress"}],
        "requested_size": "M",
        "requested_color": "black",
    }

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    reply, _history, products = asyncio.run(
        supervisor.ask(
            app,
            [],
            "Check nearby stores for Black Midi Dress in black, size M",
            conversation_context=context,
        )
    )

    assert reply == "Which store or ZIP code should I use to check nearby availability?"
    assert products == []
    assert app.graph_calls == 0
    assert app.scout_specialists["inventory_agent"].calls == 0
    assert context["pending_store_availability_product_id"] == "P001"


def test_zip_reply_continues_pending_nearby_store_check(monkeypatch):
    app = App()
    context = {
        "active_product_id": "P001",
        "active_product_name": "Black Midi Dress",
        "active_selected_products": [{"product_id": "P001", "name": "Black Midi Dress"}],
        "requested_size": "M",
        "requested_color": "black",
        "pending_store_availability_product_id": "P001",
    }
    captured = {}

    async def fake_tool(tool_name, args, *, agent_name):
        captured.update(tool_name=tool_name, args=args, agent_name=agent_name)

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(reply="Store verified."))

    asyncio.run(
        supervisor.ask(app, [], "55678", conversation_context=context)
    )

    assert captured == {
        "tool_name": "stores",
        "args": {"product_id": "P001", "store_name": "55678", "size": "M", "color": "black"},
        "agent_name": "inventory_agent",
    }
    assert context["requested_store"] == "55678"
    assert context["pending_store_availability_product_id"] is None


def test_named_store_followup_preserves_exact_variant(monkeypatch):
    app = App()
    context = {
        "active_product_id": "P001",
        "active_product_name": "Black Midi Dress",
        "active_selected_products": [{"product_id": "P001", "name": "Black Midi Dress"}],
        "requested_size": "M",
        "requested_color": "black",
    }
    captured = {}

    async def fake_tool(tool_name, args, *, agent_name):
        captured.update(tool_name=tool_name, args=args, agent_name=agent_name)

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(reply="Store verified."))

    asyncio.run(
        supervisor.ask(
            app,
            [],
            "Check at Maple Grove for Black Midi Dress in black, size M",
            conversation_context=context,
        )
    )

    assert captured == {
        "tool_name": "stores",
        "args": {"product_id": "P001", "store_name": "Maple Grove", "size": "M", "color": "black"},
        "agent_name": "inventory_agent",
    }


def test_online_delivery_followup_preserves_exact_variant(monkeypatch):
    app = App()
    context = {
        "active_product_id": "P001",
        "active_product_name": "Black Midi Dress",
        "active_selected_products": [{"product_id": "P001", "name": "Black Midi Dress"}],
        "requested_size": "M",
        "requested_color": "black",
    }
    captured = {}

    async def fake_tool(tool_name, args, *, agent_name):
        captured.update(tool_name=tool_name, args=args, agent_name=agent_name)

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(reply="Delivery verified."))

    asyncio.run(
        supervisor.ask(
            app,
            [],
            "Check online or delivery availability for Black Midi Dress in black, size M",
            conversation_context=context,
        )
    )

    assert captured == {
        "tool_name": "fulfillment_options",
        "args": {"product_id": "P001", "size": "M", "color": "black"},
        "agent_name": "inventory_agent",
    }


def test_can_it_be_delivered_routes_to_delivery_check(monkeypatch):
    app = App()
    context = {
        "active_product_id": "P001",
        "active_product_name": "Black Midi Dress",
        "active_category": "dresses",
        "active_selected_products": [{"product_id": "P001", "name": "Black Midi Dress", "category": "dresses"}],
        "requested_size": "M",
        "requested_color": "black",
        "requested_budget_max": 80,
        "last_out_of_stock_product_id": "P001",
    }
    captured = {}

    async def fake_tool(tool_name, args, *, agent_name):
        captured.update(tool_name=tool_name, args=args, agent_name=agent_name)

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(reply="Delivery verified."))

    asyncio.run(supervisor.ask(app, [], "Can it be delivered?", conversation_context=context))

    assert captured == {
        "tool_name": "fulfillment_options",
        "args": {"product_id": "P001", "size": "M", "color": "black"},
        "agent_name": "inventory_agent",
    }
    assert app.graph_calls == 0
    assert app.scout_specialists["inventory_agent"].calls == 0


def test_delivery_recovery_paraphrases_route_to_delivery_only(monkeypatch):
    for message in ("Could you ship it to me?", "Can you send it to me?"):
        app = App()
        context = {
            "active_product_id": "P001",
            "active_product_name": "Black Midi Dress",
            "active_category": "dresses",
            "active_selected_products": [{"product_id": "P001", "name": "Black Midi Dress", "category": "dresses"}],
            "requested_size": "M",
            "requested_color": "black",
            "requested_budget_max": 80,
            "last_out_of_stock_product_id": "P001",
        }
        captured = {}

        async def fake_tool(tool_name, args, *, agent_name):
            captured.update(tool_name=tool_name, args=args, agent_name=agent_name)

        monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
        monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
        monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(reply="Delivery verified."))

        asyncio.run(supervisor.ask(app, [], message, conversation_context=context))

        assert captured == {
            "tool_name": "fulfillment_options",
            "args": {"product_id": "P001", "size": "M", "color": "black"},
            "agent_name": "inventory_agent",
        }
        assert app.graph_calls == 0
        assert app.scout_specialists["inventory_agent"].calls == 0


def test_today_recovery_without_location_asks_for_location(monkeypatch):
    app = App()
    context = {
        "active_product_id": "P001",
        "active_product_name": "Black Midi Dress",
        "active_category": "dresses",
        "active_selected_products": [{"product_id": "P001", "name": "Black Midi Dress", "category": "dresses"}],
        "requested_size": "M",
        "requested_color": "black",
        "requested_budget_max": 80,
        "last_out_of_stock_product_id": "P001",
    }

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    reply, _history, products = asyncio.run(
        supervisor.ask(app, [], "I need it today", conversation_context=context)
    )

    assert reply == "Which store or ZIP code should I use to check nearby availability?"
    assert products == []
    assert app.graph_calls == 0
    assert all(agent.calls == 0 for agent in app.scout_specialists.values())


def test_urgency_paraphrase_without_location_asks_for_location(monkeypatch):
    app = App()
    context = {
        "active_product_id": "P001",
        "active_product_name": "Black Midi Dress",
        "active_category": "dresses",
        "active_selected_products": [{"product_id": "P001", "name": "Black Midi Dress", "category": "dresses"}],
        "requested_size": "M",
        "requested_color": "black",
        "requested_budget_max": 80,
        "last_out_of_stock_product_id": "P001",
    }

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    reply, _history, products = asyncio.run(
        supervisor.ask(app, [], "I need this before tonight", conversation_context=context)
    )

    assert reply == "Which store or ZIP code should I use to check nearby availability?"
    assert products == []
    assert app.graph_calls == 0
    assert all(agent.calls == 0 for agent in app.scout_specialists.values())


def test_today_recovery_with_store_preserves_exact_variant(monkeypatch):
    app = App()
    context = {
        "active_product_id": "P001",
        "active_product_name": "Black Midi Dress",
        "active_category": "dresses",
        "active_selected_products": [{"product_id": "P001", "name": "Black Midi Dress", "category": "dresses"}],
        "requested_size": "M",
        "requested_color": "black",
        "requested_store": "Maple Grove",
        "requested_budget_max": 80,
        "last_out_of_stock_product_id": "P001",
    }
    calls = []

    async def fake_tool(tool_name, args, *, agent_name):
        calls.append({"tool_name": tool_name, "args": args, "agent_name": agent_name})

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(reply="Store verified."))

    asyncio.run(supervisor.ask(app, [], "I need it today", conversation_context=context))

    assert calls == [
        {"tool_name": "stock", "args": {"product_id": "P001", "size": "M", "color": "black"}, "agent_name": "inventory_agent"},
        {
            "tool_name": "stores",
            "args": {"product_id": "P001", "store_name": "Maple Grove", "size": "M", "color": "black"},
            "agent_name": "inventory_agent",
        },
    ]
    assert app.graph_calls == 0
    assert app.scout_specialists["inventory_agent"].calls == 0


def test_no_drive_recovery_chooses_delivery_not_nearby(monkeypatch):
    app = App()
    context = {
        "active_product_id": "P001",
        "active_product_name": "Black Midi Dress",
        "active_category": "dresses",
        "active_selected_products": [{"product_id": "P001", "name": "Black Midi Dress", "category": "dresses"}],
        "requested_size": "M",
        "requested_color": "black",
        "requested_store": "Maple Grove",
        "requested_budget_max": 80,
        "last_out_of_stock_product_id": "P001",
    }
    captured = {}

    async def fake_tool(tool_name, args, *, agent_name):
        captured.update(tool_name=tool_name, args=args, agent_name=agent_name)

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(reply="Delivery verified."))

    asyncio.run(supervisor.ask(app, [], "I don't want to drive", conversation_context=context))

    assert captured == {
        "tool_name": "fulfillment_options",
        "args": {"product_id": "P001", "size": "M", "color": "black"},
        "agent_name": "inventory_agent",
    }
    assert app.graph_calls == 0
    assert app.scout_specialists["inventory_agent"].calls == 0


def test_travel_avoidance_paraphrase_skips_nearby_and_chooses_delivery(monkeypatch):
    app = App()
    context = {
        "active_product_id": "P001",
        "active_product_name": "Black Midi Dress",
        "active_category": "dresses",
        "active_selected_products": [{"product_id": "P001", "name": "Black Midi Dress", "category": "dresses"}],
        "requested_size": "M",
        "requested_color": "black",
        "requested_store": "Maple Grove",
        "requested_budget_max": 80,
        "last_out_of_stock_product_id": "P001",
    }
    captured = {}

    async def fake_tool(tool_name, args, *, agent_name):
        captured.update(tool_name=tool_name, args=args, agent_name=agent_name)

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(reply="Delivery verified."))

    asyncio.run(supervisor.ask(app, [], "I'd rather not go to another store", conversation_context=context))

    assert captured == {
        "tool_name": "fulfillment_options",
        "args": {"product_id": "P001", "size": "M", "color": "black"},
        "agent_name": "inventory_agent",
    }
    assert app.graph_calls == 0
    assert app.scout_specialists["inventory_agent"].calls == 0


def test_any_way_to_get_it_chooses_single_delivery_check(monkeypatch):
    app = App()
    context = {
        "active_product_id": "P001",
        "active_product_name": "Black Midi Dress",
        "active_category": "dresses",
        "active_selected_products": [{"product_id": "P001", "name": "Black Midi Dress", "category": "dresses"}],
        "requested_size": "M",
        "requested_color": "black",
        "requested_budget_max": 80,
        "last_out_of_stock_product_id": "P001",
    }
    calls = []

    async def fake_tool(tool_name, args, *, agent_name):
        calls.append({"tool_name": tool_name, "args": args, "agent_name": agent_name})

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(reply="Delivery verified."))

    asyncio.run(supervisor.ask(app, [], "Is there any way I can get it?", conversation_context=context))

    assert calls == [
        {
            "tool_name": "fulfillment_options",
            "args": {"product_id": "P001", "size": "M", "color": "black"},
            "agent_name": "inventory_agent",
        }
    ]
    assert app.graph_calls == 0
    assert app.scout_specialists["inventory_agent"].calls == 0


def test_any_way_to_get_it_with_known_store_chooses_pickup_check(monkeypatch):
    app = App()
    context = {
        "active_product_id": "P001",
        "active_product_name": "Black Midi Dress",
        "active_category": "dresses",
        "active_selected_products": [{"product_id": "P001", "name": "Black Midi Dress", "category": "dresses"}],
        "requested_size": "M",
        "requested_color": "black",
        "requested_store": "Maple Grove",
        "requested_budget_max": 80,
        "last_out_of_stock_product_id": "P001",
    }
    calls = []

    async def fake_tool(tool_name, args, *, agent_name):
        calls.append({"tool_name": tool_name, "args": args, "agent_name": agent_name})

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(reply="Store verified."))

    asyncio.run(supervisor.ask(app, [], "Is there any way I can get it?", conversation_context=context))

    assert calls == [
        {"tool_name": "stock", "args": {"product_id": "P001", "size": "M", "color": "black"}, "agent_name": "inventory_agent"},
        {
            "tool_name": "stores",
            "args": {"product_id": "P001", "store_name": "Maple Grove", "size": "M", "color": "black"},
            "agent_name": "inventory_agent",
        },
    ]
    assert app.graph_calls == 0
    assert app.scout_specialists["inventory_agent"].calls == 0


def test_check_another_store_without_location_asks_for_location(monkeypatch):
    app = App()
    context = {
        "active_product_id": "P001",
        "active_product_name": "Black Midi Dress",
        "active_category": "dresses",
        "active_selected_products": [{"product_id": "P001", "name": "Black Midi Dress", "category": "dresses"}],
        "requested_size": "M",
        "requested_color": "black",
        "requested_budget_max": 80,
        "last_out_of_stock_product_id": "P001",
    }

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    reply, _history, products = asyncio.run(
        supervisor.ask(app, [], "Check another store", conversation_context=context)
    )

    assert reply == "Which store or ZIP code should I use to check nearby availability?"
    assert products == []
    assert app.graph_calls == 0
    assert all(agent.calls == 0 for agent in app.scout_specialists.values())


def test_location_paraphrases_without_location_ask_for_location(monkeypatch):
    for message in ("Does another location have it?", "Do any other locations have it?"):
        app = App()
        context = {
            "active_product_id": "P001",
            "active_product_name": "Black Midi Dress",
            "active_category": "dresses",
            "active_selected_products": [{"product_id": "P001", "name": "Black Midi Dress", "category": "dresses"}],
            "requested_size": "M",
            "requested_color": "black",
            "requested_budget_max": 80,
            "last_out_of_stock_product_id": "P001",
        }

        monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
        reply, _history, products = asyncio.run(
            supervisor.ask(app, [], message, conversation_context=context)
        )

        assert reply == "Which store or ZIP code should I use to check nearby availability?"
        assert products == []
        assert app.graph_calls == 0
        assert all(agent.calls == 0 for agent in app.scout_specialists.values())


def test_check_another_store_with_known_store_preserves_exact_variant(monkeypatch):
    app = App()
    context = {
        "active_product_id": "P001",
        "active_product_name": "Black Midi Dress",
        "active_category": "dresses",
        "active_selected_products": [{"product_id": "P001", "name": "Black Midi Dress", "category": "dresses"}],
        "requested_size": "M",
        "requested_color": "black",
        "requested_store": "Maple Grove",
        "requested_budget_max": 80,
        "last_out_of_stock_product_id": "P001",
    }
    calls = []

    async def fake_tool(tool_name, args, *, agent_name):
        calls.append({"tool_name": tool_name, "args": args, "agent_name": agent_name})

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(reply="Store verified."))

    asyncio.run(supervisor.ask(app, [], "Check another store", conversation_context=context))

    assert calls == [
        {"tool_name": "stock", "args": {"product_id": "P001", "size": "M", "color": "black"}, "agent_name": "inventory_agent"},
        {
            "tool_name": "stores",
            "args": {"product_id": "P001", "store_name": "Maple Grove", "size": "M", "color": "black"},
            "agent_name": "inventory_agent",
        },
    ]
    assert app.graph_calls == 0
    assert app.scout_specialists["inventory_agent"].calls == 0


def test_find_similar_followup_preserves_category_variant_and_budget(monkeypatch):
    app = App()
    context = {
        "active_product_id": "P001",
        "active_product_name": "Black Midi Dress",
        "active_category": "dresses",
        "active_selected_products": [{"product_id": "P001", "name": "Black Midi Dress", "category": "dresses"}],
        "requested_size": "M",
        "requested_color": "black",
        "requested_budget_max": 80,
        "last_out_of_stock_product_id": "P001",
    }
    captured = {}

    async def fake_tool(tool_name, args, *, agent_name):
        captured.update(tool_name=tool_name, args=args, agent_name=agent_name)

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(reply="Similar products verified."))

    asyncio.run(
        supervisor.ask(
            app,
            [],
            "Find similar products",
            conversation_context=context,
        )
    )

    assert captured == {
        "tool_name": "alternatives",
        "args": {
            "product_id": "P001",
            "limit": 3,
            "size": "M",
            "color": "black",
            "max_price": 80.0,
            "category": "dresses",
        },
        "agent_name": "recommend_agent",
    }
    assert app.graph_calls == 0
    assert app.scout_specialists["recommend_agent"].calls == 0


def test_show_me_something_similar_routes_to_alternatives(monkeypatch):
    app = App()
    context = {
        "active_product_id": "P001",
        "active_product_name": "Black Midi Dress",
        "active_category": "dresses",
        "active_selected_products": [{"product_id": "P001", "name": "Black Midi Dress", "category": "dresses"}],
        "requested_size": "M",
        "requested_color": "black",
        "requested_budget_max": 80,
        "last_out_of_stock_product_id": "P001",
    }
    captured = {}

    async def fake_tool(tool_name, args, *, agent_name):
        captured.update(tool_name=tool_name, args=args, agent_name=agent_name)

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(reply="Similar products verified."))

    asyncio.run(supervisor.ask(app, [], "Show me something similar", conversation_context=context))

    assert captured == {
        "tool_name": "alternatives",
        "args": {
            "product_id": "P001",
            "limit": 3,
            "size": "M",
            "color": "black",
            "max_price": 80.0,
            "category": "dresses",
        },
        "agent_name": "recommend_agent",
    }
    assert app.graph_calls == 0
    assert app.scout_specialists["recommend_agent"].calls == 0


def test_similar_product_paraphrases_route_to_alternatives(monkeypatch):
    for message in ("Anything else like this?", "Are there other options like it?"):
        app = App()
        context = {
            "active_product_id": "P001",
            "active_product_name": "Black Midi Dress",
            "active_category": "dresses",
            "active_selected_products": [{"product_id": "P001", "name": "Black Midi Dress", "category": "dresses"}],
            "requested_size": "M",
            "requested_color": "black",
            "requested_budget_max": 80,
            "last_out_of_stock_product_id": "P001",
        }
        captured = {}

        async def fake_tool(tool_name, args, *, agent_name):
            captured.update(tool_name=tool_name, args=args, agent_name=agent_name)

        monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
        monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
        monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(reply="Similar products verified."))

        asyncio.run(supervisor.ask(app, [], message, conversation_context=context))

        assert captured == {
            "tool_name": "alternatives",
            "args": {
                "product_id": "P001",
                "limit": 3,
                "size": "M",
                "color": "black",
                "max_price": 80.0,
                "category": "dresses",
            },
            "agent_name": "recommend_agent",
        }
        assert app.graph_calls == 0
        assert app.scout_specialists["recommend_agent"].calls == 0


def test_find_similar_black_medium_no_scout_results_returns_clear_message(monkeypatch):
    app = App()
    context = {
        "active_product_id": "P001",
        "active_product_name": "Black Midi Dress",
        "active_category": "dresses",
        "active_selected_products": [{"product_id": "P001", "name": "Black Midi Dress", "category": "dresses"}],
        "requested_size": "M",
        "requested_color": "black",
        "requested_budget_max": 80,
        "last_out_of_stock_product_id": "P001",
    }

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    reply, _history, products = asyncio.run(
        supervisor.ask(
            app,
            [],
            "Find similar products",
            conversation_context=context,
        )
    )

    assert reply == "I couldn’t find a matching alternative to Black Midi Dress matching black, medium, under $80."
    assert products == []
    assert app.graph_calls == 0
    assert app.scout_specialists["recommend_agent"].calls == 0


def test_tool_first_retains_color_constraint(monkeypatch):
    app = App()
    context = {
        "active_product_id": "P001",
        "active_product_name": "Black Midi Dress",
        "active_selected_products": [{"product_id": "P001", "name": "Black Midi Dress"}],
    }
    captured = {}

    async def fake_tool(tool_name, args, *, agent_name):
        captured.update(tool_name=tool_name, args=args, agent_name=agent_name)

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(reply="Inventory verified."))

    asyncio.run(supervisor.ask(app, [], "Is the black one available in medium?", conversation_context=context))

    assert captured["tool_name"] == "stock"
    assert captured["args"] == {"product_id": "P001", "size": "M", "color": "black"}
    assert app.scout_specialists["inventory_agent"].calls == 0


def test_ambiguous_multiple_active_products_trigger_clarification(monkeypatch):
    app = App()
    context = {
        "active_selected_products": [
            {"product_id": "P001", "name": "Black Midi Dress"},
            {"product_id": "P002", "name": "Red Dress"},
        ]
    }
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())

    reply, _history, products = asyncio.run(supervisor.ask(app, [], "Is it available in medium?", conversation_context=context))

    assert "which product" in reply.lower()
    assert products == []
    assert app.graph_calls == 0
    assert all(agent.calls == 0 for agent in app.scout_specialists.values())


def test_no_active_product_does_not_guess(monkeypatch):
    app = App()
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(reply="Graph path."))

    asyncio.run(supervisor.ask(app, [], "Is it available?", conversation_context={}))

    assert app.graph_calls == 1


def test_rejected_product_claims_do_not_enter_context():
    context = {}
    rejected = claim(ClaimType.PRODUCT_IDENTITY.value, "P999", "name", "Invented Coat", "c1")

    supervisor._update_context_from_verified_turn(
        context,
        finalized=finalized(claims=[rejected], approved=[]),
        structured_intent=StructuredIntent(text="request", request_type="product_recommendation", confidence=0.9),
    )

    assert context["active_product_id"] is None
    assert context["active_selected_products"] == []


def test_verified_order_claim_becomes_active_order_context():
    context = {}
    order_status = claim(ClaimType.ORDER_STATUS.value, "O1001", "status", "shipped", "c_order")

    supervisor._update_context_from_verified_turn(
        context,
        finalized=finalized(claims=[order_status]),
        structured_intent=StructuredIntent(text="Where is O1001?", request_type="order_status", confidence=0.9),
    )

    assert context["active_order_id"] == "O1001"


def test_sessions_remain_isolated(monkeypatch):
    first = {}
    second = {}
    product = {"product_id": "P001", "name": "Black Midi Dress", "source": "internal"}

    supervisor._update_context_from_verified_turn(
        first,
        finalized=finalized(products=[product]),
        structured_intent=StructuredIntent(text="request", request_type="product_recommendation", confidence=0.9),
    )

    assert first["active_product_id"] == "P001"
    assert second == {}


def test_order_return_followup_uses_active_order_id(monkeypatch):
    app = App()
    context = {"active_order_id": "O1001"}
    captured = {}

    async def fake_tool(tool_name, args, *, agent_name):
        captured.update(tool_name=tool_name, args=args, agent_name=agent_name)

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(reply="Return verified."))

    reply, _history, products = asyncio.run(supervisor.ask(app, [], "Can I return it?", conversation_context=context))

    assert reply == "Return verified."
    assert products == []
    assert app.graph_calls == 0
    assert app.scout_specialists["order_agent"].calls == 0
    assert captured == {
        "tool_name": "return_eligibility",
        "args": {"order_id": "O1001"},
        "agent_name": "order_agent",
    }


def test_order_arrival_followup_uses_active_order_id(monkeypatch):
    app = App()
    context = {"active_order_id": "O1001"}
    captured = {}

    async def fake_tool(tool_name, args, *, agent_name):
        captured.update(tool_name=tool_name, args=args, agent_name=agent_name)

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(reply="Order verified."))

    asyncio.run(supervisor.ask(app, [], "When will it arrive?", conversation_context=context))

    assert app.graph_calls == 0
    assert app.scout_specialists["order_agent"].calls == 0
    # "When will it arrive?" is genuinely an arrival/shipping question -
    # as of Phase 3 (shipment tracking), this correctly calls the richer
    # shipment_status tool instead of plain order status.
    assert captured == {"tool_name": "shipment_status", "args": {"order_id": "O1001"}, "agent_name": "order_agent"}


def test_order_items_followup_uses_active_order_id(monkeypatch):
    app = App()
    context = {"active_order_id": "O1001"}
    captured = {}

    async def fake_tool(tool_name, args, *, agent_name):
        captured.update(tool_name=tool_name, args=args, agent_name=agent_name)

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(reply="Items verified."))

    asyncio.run(supervisor.ask(app, [], "What did I order?", conversation_context=context))

    assert app.graph_calls == 0
    assert app.scout_specialists["order_agent"].calls == 0
    # "What did I order?" asks about order CONTENTS, not shipping status -
    # correctly still uses the plain orders tool, not shipment_status.
    assert captured == {"tool_name": "orders", "args": {"order_id": "O1001"}, "agent_name": "order_agent"}


def test_broad_shoe_request_returns_deterministic_clarification(monkeypatch):
    app = App()
    context = {}
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())

    reply, _history, products = asyncio.run(supervisor.ask(app, [], "Show me some shoes", conversation_context=context))

    assert "shoes" in reply.lower()
    assert "budget" in reply.lower()
    assert products == []
    assert context["active_category"] == "shoes"
    assert context["pending_intent"] == "product_recommendation"


def test_broad_occasion_clarification_stores_pending_context(monkeypatch):
    app = App()
    context = {}
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())

    reply, _history, products = asyncio.run(supervisor.ask(app, [], "I need something nice for a party.", conversation_context=context))

    assert reply == "What type of item are you looking for, and what budget would you like to stay within?"
    assert products == []
    assert context["pending_intent"] == "product_recommendation"
    assert context["pending_missing_fields"] == ["item_type", "budget"]
    assert app.graph_calls == 0
    assert all(agent.calls == 0 for agent in app.scout_specialists.values())


def test_category_only_followup_asks_for_budget_without_model_or_agent(monkeypatch):
    app = App()
    context = {
        "pending_intent": "product_recommendation",
        "pending_missing_fields": ["item_type", "budget"],
    }
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())

    reply, _history, products = asyncio.run(supervisor.ask(app, [], "dress and shoes", conversation_context=context))

    assert reply == "Got it — dresses and shoes. What budget should I use?"
    assert products == []
    assert context["active_category"] == "dresses and shoes"
    assert context["pending_intent"] == "product_recommendation"
    assert context["pending_missing_fields"] == ["budget"]
    assert app.graph_calls == 0
    assert all(agent.calls == 0 for agent in app.scout_specialists.values())


def test_category_followup_with_shopping_filler_still_asks_for_budget(monkeypatch):
    app = App()
    context = {
        "pending_intent": "product_recommendation",
        "pending_missing_fields": ["item_type", "budget"],
    }
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())

    reply, _history, products = asyncio.run(
        supervisor.ask(app, [], "show me some nice dress and shoes", conversation_context=context)
    )

    assert reply == "Got it — dresses and shoes. What budget should I use?"
    assert products == []
    assert context["active_category"] == "dresses and shoes"
    assert context["pending_missing_fields"] == ["budget"]
    assert app.graph_calls == 0
    assert all(agent.calls == 0 for agent in app.scout_specialists.values())


def test_hiking_followup_merges_pending_context_and_routes_recommend(monkeypatch):
    # Updated for the new deterministic tool-first recommendation path
    # (see tool_first.py) - a clear, merged recommendation request like
    # this now correctly bypasses the specialist agent (and therefore
    # the model) entirely, calling recommend_products directly. This is
    # a genuine improvement: it removes a real, live-confirmed source of
    # intermittent failure where the model would sometimes decline to
    # call any tool for an unambiguous recommendation request.
    app = App()
    context = {
        "active_category": "shoes",
        "pending_intent": "product_recommendation",
        "pending_missing_fields": ["use_case", "budget"],
    }
    # Return a real, non-empty result here - an EMPTY result correctly
    # falls through to the real agent instead (needed for the
    # NEEDS_EXTERNAL_CHECK handoff to external_offer_agent; see
    # tool_first.py), which is a separate, real scenario covered
    # elsewhere. This test's purpose is confirming the deterministic
    # path is used at all for a merged, clear recommendation.
    captured = {}

    async def fake_tool(tool_name, args, *, agent_name):
        captured.update(tool_name=tool_name, args=args, agent_name=agent_name)
        return [{"product_id": "P999", "name": "Trail Runner", "price": 89.99}]

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)

    asyncio.run(supervisor.ask(app, [], "Hiking", conversation_context=context))

    assert app.graph_calls == 0
    assert app.scout_specialists["recommend_agent"].calls == 0
    assert captured.get("tool_name") == "recommend_products"
    assert captured.get("agent_name") == "recommend_agent"
    assert "hiking" in captured.get("args", {}).get("query", "").lower()


def test_unrelated_followup_does_not_merge_pending_context(monkeypatch):
    app = App()
    context = {"active_category": "shoes", "pending_intent": "product_recommendation"}
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(reply="Policy verified."))

    asyncio.run(supervisor.ask(app, [], "What is your return policy?", conversation_context=context))

    assert app.scout_specialists["policy_agent"].calls == 1
    assert app.scout_specialists["recommend_agent"].calls == 0


def test_current_explicit_request_overrides_pending_context(monkeypatch):
    # Updated for the new deterministic tool-first recommendation path -
    # a clear, explicit recommendation now correctly bypasses the
    # specialist agent entirely (see tool_first.py).
    app = App()
    context = {"active_category": "shoes", "pending_intent": "product_recommendation"}
    captured = {}

    async def fake_tool(tool_name, args, *, agent_name):
        captured.update(tool_name=tool_name, args=args, agent_name=agent_name)
        return [{"product_id": "P001", "name": "Wrap Dress", "price": 68.0}]

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)

    asyncio.run(supervisor.ask(app, [], "Recommend a dress under $80.", conversation_context=context))

    assert app.scout_specialists["recommend_agent"].calls == 0
    assert captured.get("tool_name") == "recommend_products"
    assert "dress" in captured.get("args", {}).get("query", "").lower()


def test_pending_clarification_clears_after_successful_completion(monkeypatch):
    app = App()
    context = {
        "active_category": "shoes",
        "pending_intent": "product_recommendation",
        "pending_missing_fields": ["use_case", "budget"],
    }
    product = {"product_id": "P010", "name": "Trail Shoe", "price": 69.0, "source": "internal"}

    async def fake_tool(tool_name, args, *, agent_name):
        return [product]

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(products=[product]))

    asyncio.run(supervisor.ask(app, [], "Hiking", conversation_context=context))

    assert context["pending_intent"] is None
    assert context["pending_missing_fields"] == []


class ExternalEvidenceAgent:
    def __init__(self):
        self.later_model_started = False

    async def astream_events(self, payload, version, config):
        yield {"event": "on_chat_model_start", "run_id": "m1", "data": {"input": {"messages": payload["messages"]}}}
        yield {"event": "on_chat_model_end", "run_id": "m1", "data": {}}
        record_tool_call(
            tool_name="search_external_offers",
            validated_args={"query": "Do you have red cocktail dresses under $50?", "category": "dresses", "budget_max": 50},
            success=True,
            result=[
                {
                    "external_product_id": "EX011",
                    "name": "Red Cocktail Dress",
                    "vendor_name": "Target",
                    "price": 45.0,
                    "click_url": "/affiliate/click/EX011",
                    "source": "external",
                }
            ],
            agent_name="external_offer_agent",
        )
        yield {"event": "on_tool_end", "run_id": "t1", "data": {}}
        self.later_model_started = True
        yield {"event": "on_chat_model_start", "run_id": "m2", "data": {"input": {"messages": payload["messages"]}}}


def test_explicit_external_request_routes_directly_and_stops_final_model(monkeypatch):
    app = App()
    agent = ExternalEvidenceAgent()
    app.scout_specialists["external_offer_agent"] = agent
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())

    reply, _history, products = asyncio.run(
        supervisor.ask(app, [], "Show me third-party alternatives for red dresses under $50.")
    )

    # Behavior check: a real, non-Scout option was found and clearly
    # framed as such - exact phrasing may evolve independently.
    assert "option" in reply.lower() and "retailer" in reply.lower()
    assert products[0]["source"] == "external"
    assert app.graph_calls == 0
    assert not agent.later_model_started


def test_explicit_external_request_tool_first_searches_external(monkeypatch):
    app = App()
    captured = {}

    async def fake_tool(tool_name, args, *, agent_name):
        captured.update(tool_name=tool_name, args=args, agent_name=agent_name)

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(reply="External verified.", products=[{"external_product_id": "EX1", "name": "Offer", "source": "external"}]))

    reply, _history, products = asyncio.run(
        supervisor.ask(app, [], "Show me third-party alternatives for red dresses under $50.")
    )

    assert reply == "External verified."
    assert captured["tool_name"] == "search_external_offers"
    assert captured["agent_name"] == "external_offer_agent"
    assert products[0]["source"] == "external"
    assert "product_id" not in products[0]


def test_internal_search_does_not_route_externally(monkeypatch):
    app = App()
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    captured = {}

    async def fake_tool(tool_name, args, *, agent_name):
        captured.update(tool_name=tool_name, args=args, agent_name=agent_name)
        return [{"product_id": "P001", "name": "Wrap Dress", "price": 68.0}]

    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)

    asyncio.run(supervisor.ask(app, [], "Recommend a dress under $80."))

    assert app.scout_specialists["recommend_agent"].calls == 0
    assert captured.get("tool_name") == "recommend_products"
    assert app.scout_specialists["external_offer_agent"].calls == 0


def test_unique_search_result_continues_to_stock(monkeypatch):
    captured = {}

    async def fake_tool(tool_name, args, *, agent_name):
        captured.update(tool_name=tool_name, args=args, agent_name=agent_name)

    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    start_evidence_context("sub_search")
    try:
        record_tool_call(
            tool_name="search",
            validated_args={"query": "black midi dress"},
            success=True,
            result=[{"product_id": "P001", "name": "Black Midi Dress", "price": 79.99}],
            agent_name="inventory_agent",
        )
        result = asyncio.run(
            supervisor._continue_after_tool_evidence(
                structured_intent=StructuredIntent(
                    text="Is the black midi dress in medium?",
                    request_type="inventory_availability",
                    confidence=0.9,
                    size="M",
                    color="black",
                ),
                agent_name="inventory_agent",
            )
        )
    finally:
        clear_evidence_context()

    assert result is True
    assert captured == {"tool_name": "stock", "args": {"product_id": "P001", "size": "M", "color": "black"}, "agent_name": "inventory_agent"}


def test_unique_search_result_continues_to_stores(monkeypatch):
    captured = {}

    async def fake_tool(tool_name, args, *, agent_name):
        captured.update(tool_name=tool_name, args=args, agent_name=agent_name)

    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    start_evidence_context("sub_store")
    try:
        record_tool_call(
            tool_name="search",
            validated_args={"query": "black midi dress"},
            success=True,
            result=[{"product_id": "P001", "name": "Black Midi Dress", "price": 79.99}],
            agent_name="inventory_agent",
        )
        result = asyncio.run(
            supervisor._continue_after_tool_evidence(
                structured_intent=StructuredIntent(
                    text="Is it available at Maple Grove?",
                    request_type="store_availability",
                    confidence=0.9,
                    location="Maple Grove",
                ),
                agent_name="inventory_agent",
            )
        )
    finally:
        clear_evidence_context()

    assert result is True
    assert captured == {"tool_name": "stores", "args": {"product_id": "P001", "store_name": "Maple Grove"}, "agent_name": "inventory_agent"}


def test_multiple_or_failed_search_does_not_continue(monkeypatch):
    calls = []

    async def fake_tool(tool_name, args, *, agent_name):
        calls.append((tool_name, args, agent_name))

    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    for success, result in (
        (True, [{"product_id": "P001", "name": "A"}, {"product_id": "P002", "name": "B"}]),
        (False, []),
    ):
        clear_evidence_context()
        start_evidence_context("sub_no_continue")
        record_tool_call(
            tool_name="search",
            validated_args={"query": "dress"},
            success=success,
            result=result,
            agent_name="inventory_agent",
        )
        continued = asyncio.run(
            supervisor._continue_after_tool_evidence(
                structured_intent=StructuredIntent(
                    text="Is the dress in medium?",
                    request_type="inventory_availability",
                    confidence=0.9,
                    size="M",
                ),
                agent_name="inventory_agent",
            )
        )
        assert continued is False
    clear_evidence_context()
    assert calls == []


def test_zero_internal_matches_trigger_one_external_search(monkeypatch):
    captured = []

    async def fake_tool(tool_name, args, *, agent_name):
        captured.append((tool_name, args, agent_name))

    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    start_evidence_context("sub_external")
    try:
        record_tool_call(
            tool_name="recommend_products",
            validated_args={"query": "red cocktail dresses", "max_price": 50},
            success=True,
            result=[],
            agent_name="recommend_agent",
        )
        continued = asyncio.run(
            supervisor._continue_after_tool_evidence(
                structured_intent=StructuredIntent(
                    text="Do you have red cocktail dresses under $50?",
                    request_type="product_recommendation",
                    confidence=0.9,
                    product_type="dresses",
                    budget_max=50,
                ),
                agent_name="recommend_agent",
            )
        )
    finally:
        clear_evidence_context()

    assert continued is True
    assert captured == [("search_external_offers", {"query": "Do you have red cocktail dresses under $50?", "category": "dresses", "budget_max": 50}, "external_offer_agent")]


def test_zero_internal_search_matches_trigger_external_search(monkeypatch):
    captured = []

    async def fake_tool(tool_name, args, *, agent_name):
        captured.append((tool_name, args, agent_name))

    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    start_evidence_context("sub_external_search")
    try:
        record_tool_call(
            tool_name="search",
            validated_args={"query": "red cocktail dresses", "max_price": 50},
            success=True,
            result=[],
            agent_name="recommend_agent",
        )
        continued = asyncio.run(
            supervisor._continue_after_tool_evidence(
                structured_intent=StructuredIntent(
                    text="Do you have red cocktail dresses under $50?",
                    request_type="product_recommendation",
                    confidence=0.9,
                    product_type="dresses",
                    color="red",
                    budget_max=50,
                ),
                agent_name="recommend_agent",
            )
        )
    finally:
        clear_evidence_context()

    assert continued is True
    assert captured == [("search_external_offers", {"query": "Do you have red cocktail dresses under $50?", "category": "dresses", "budget_max": 50}, "external_offer_agent")]


def test_mismatched_internal_matches_trigger_external_search(monkeypatch):
    captured = []

    async def fake_tool(tool_name, args, *, agent_name):
        captured.append((tool_name, args, agent_name))

    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    start_evidence_context("sub_external_mismatch")
    try:
        record_tool_call(
            tool_name="recommend_products",
            validated_args={"query": "red cocktail dresses", "max_price": 50},
            success=True,
            result=[{"product_id": "P001", "name": "Black Midi Dress", "price": 49.99}],
            agent_name="recommend_agent",
        )
        continued = asyncio.run(
            supervisor._continue_after_tool_evidence(
                structured_intent=StructuredIntent(
                    text="Do you have red cocktail dresses under $50?",
                    request_type="product_recommendation",
                    confidence=0.9,
                    product_type="dresses",
                    color="red",
                    budget_max=50,
                ),
                agent_name="recommend_agent",
            )
        )
    finally:
        clear_evidence_context()

    assert continued is True
    assert captured == [("search_external_offers", {"query": "Do you have red cocktail dresses under $50?", "category": "dresses", "budget_max": 50}, "external_offer_agent")]


def test_internal_match_prevents_external_search(monkeypatch):
    calls = []

    async def fake_tool(tool_name, args, *, agent_name):
        calls.append((tool_name, args, agent_name))

    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    start_evidence_context("sub_internal")
    try:
        record_tool_call(
            tool_name="recommend_products",
            validated_args={"query": "dress"},
            success=True,
            result=[{"product_id": "P001", "name": "Black Midi Dress", "price": 79.99}],
            agent_name="recommend_agent",
        )
        continued = asyncio.run(
            supervisor._continue_after_tool_evidence(
                structured_intent=StructuredIntent(
                    text="Recommend a dress under $80.",
                    request_type="product_recommendation",
                    confidence=0.9,
                    product_type="dress",
                    budget_max=80,
                ),
                agent_name="recommend_agent",
            )
        )
    finally:
        clear_evidence_context()

    assert continued is False
    assert calls == []


def test_tool_first_results_still_require_verification(monkeypatch):
    app = App()
    called = {}

    async def fake_tool(tool_name, args, *, agent_name):
        called["tool"] = tool_name

    def capture_finalize(**kwargs):
        called["evidence_count"] = len(get_evidence_entries())
        return finalized(reply="Verified by pipeline.")

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    monkeypatch.setattr(supervisor, "_finalize_verified_response", capture_finalize)

    reply, _history, _products = asyncio.run(
        supervisor.ask(
            app,
            [],
            "Is it available in medium?",
            conversation_context={
                "active_product_id": "P001",
                "active_product_name": "Black Midi Dress",
                "active_selected_products": [{"product_id": "P001", "name": "Black Midi Dress"}],
            },
        )
    )

    assert reply == "Verified by pipeline."
    assert called["tool"] == "stock"
    assert "evidence_count" in called


def test_pickup_is_not_inferred_from_stock():
    start_evidence_context("sub_pickup")
    try:
        record_tool_call(
            tool_name="stock",
            validated_args={"product_id": "P001", "size": "M"},
            success=True,
            result={"product_id": "P001", "size": "M", "quantity": 0},
            agent_name="inventory_agent",
        )
        entries = get_evidence_entries()
    finally:
        clear_evidence_context()

    facts = entries[0].normalized_facts
    assert "pickup_available" not in facts
    assert all("pickup_available" not in item for item in facts.get("items", []))


def test_api_and_sse_schemas_remain_unchanged():
    assert set(ChatRequest.model_fields) == {"message", "session_id"}
    assert set(ChatResponse.model_fields) == {"session_id", "reply", "products"}
    assert ChatResponse.model_fields["products"].default == []


def test_topic_switch_clears_pending_cart_offer_so_later_yes_is_safe(monkeypatch):
    """Real, live-verified scenario: after a genuine topic switch (a
    policy question, not a clarification about the same product), the
    pending cart offer must be cleared - a later, unrelated 'yes' must
    NOT silently confirm a stale cart-add. Confirmed live tonight: the
    sequence recommend -> select second item -> ask about return policy
    -> 'yes' correctly returns an honest 'couldn't verify' rather than
    adding the stale Slip Dress to the cart.
    """
    app = App()
    context = {
        "active_selected_products": [
            {"product_id": "P001", "name": "Black Midi Dress", "category": "dresses"},
            {"product_id": "P004", "name": "Slip Dress", "category": "dresses"},
        ],
        "pending_cart_offer": {
            "product_id": "P004",
            "product_name": "Slip Dress",
            "size": None,
            "color": None,
            "quantity": 1,
            "recommendation_id": None,
        },
    }
    captured = {}

    async def fake_tool(tool_name, args, *, agent_name):
        captured.update(tool_name=tool_name, args=args, agent_name=agent_name)
        return []

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)

    # A genuine topic switch - a policy question, not a clarification
    # about the Slip Dress - must clear the pending offer.
    asyncio.run(
        supervisor.ask(app, [], "What is your return policy?", conversation_context=context)
    )

    assert context.get("pending_cart_offer") is None

    # A later "yes" must not silently confirm the (now-cleared) stale offer.
    reply, _history, _products = asyncio.run(
        supervisor.ask(app, [], "yes", conversation_context=context)
    )
    assert "added" not in reply.lower()
    assert "slip dress" not in reply.lower()
