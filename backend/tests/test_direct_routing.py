import asyncio
import json
from types import SimpleNamespace

from scout.agents import supervisor
from scout.agents.diagnostics import SAFE_TIMEOUT_REPLY, get_diagnostics
from scout.agents.evidence import record_tool_call
from scout.agents.intent_splitter import classify_clear_single_intent
from scout.api.chat import ChatRequest, ChatResponse


def ai(name, content):
    return SimpleNamespace(name=name, content=content, type="ai", tool_calls=None)


def tool(name, content):
    return SimpleNamespace(name=name, content=content, type="tool")


class FakeAgent:
    def __init__(self, name, content="Supported reply.", products=None, delay=None):
        self.name = name
        self.calls = 0
        self.delay = delay
        self.products = products or []
        self.content = content

    async def ainvoke(self, payload, config):
        self.calls += 1
        if self.delay:
            await asyncio.sleep(self.delay)
        messages = [*payload["messages"]]
        if self.products:
            messages.append(tool("recommend_products", json.dumps(self.products)))
        messages.append(ai(self.name, self.content))
        return {"messages": messages}


class FakeApp:
    def __init__(self):
        self.graph_calls = 0
        self.scout_specialists = {
            "recommend_agent": FakeAgent("recommend_agent"),
            "inventory_agent": FakeAgent("inventory_agent"),
            "order_agent": FakeAgent("order_agent"),
            "external_offer_agent": FakeAgent("external_offer_agent"),
            "policy_agent": FakeAgent("policy_agent"),
        }

    async def ainvoke(self, payload, config):
        self.graph_calls += 1
        return {"messages": [*payload["messages"], ai("policy_agent", "Graph reply.")]}


class EvidenceCompleteAgent:
    def __init__(self, tool_name, result, agent_name="recommend_agent"):
        self.tool_name = tool_name
        self.result = result
        self.agent_name = agent_name
        self.later_model_started = False

    async def astream_events(self, payload, version, config):
        yield {"event": "on_chat_model_start", "run_id": "m1", "data": {"input": {"messages": payload["messages"]}}}
        yield {"event": "on_chat_model_end", "run_id": "m1", "data": {}}
        record_tool_call(
            tool_name=self.tool_name,
            validated_args={"query": "dress"},
            success=True,
            result=self.result,
            agent_name=self.agent_name,
        )
        yield {"event": "on_tool_end", "run_id": "t1", "data": {}}
        self.later_model_started = True
        yield {"event": "on_chat_model_start", "run_id": "m2", "data": {"input": {"messages": payload["messages"]}}}


def finalized(reply="Verified reply.", products=None):
    return supervisor.FinalizedResponse(
        reply=reply,
        products=products or [],
        proposed_claims=[],
        verification_result=SimpleNamespace(verified=True, approved_claim_ids=[], rejected_claims=[], correction_agent=None),
    )


def test_direct_route_mapping_for_supported_fast_path_intents():
    app = FakeApp()
    cases = {
        "Recommend a dress under $80.": "recommend_agent",
        "Is the black midi dress available in medium?": "inventory_agent",
        "Is this available at Maple Grove?": "inventory_agent",
        "Where is order ORD-1001?": "order_agent",
        "Can I return order ORD-1001?": "order_agent",
        "What is your return policy?": "policy_agent",
        "Show me third-party alternatives for red dresses.": "external_offer_agent",
    }

    for message, specialist in cases.items():
        intent = classify_clear_single_intent(message)
        assert supervisor._direct_route_agent(app, intent, [message]) == specialist


def test_greeting_and_thanks_avoid_specialist_and_supervisor(monkeypatch):
    app = FakeApp()
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())

    reply, history, products = asyncio.run(supervisor.ask(app, [], "Thanks."))

    assert reply == "You're welcome — happy to help."
    assert products == []
    assert app.graph_calls == 0
    assert all(agent.calls == 0 for agent in app.scout_specialists.values())
    assert [item["role"] for item in history] == ["user", "assistant"]


def test_purchase_execution_boundary_avoids_models_and_tools(monkeypatch):
    app = FakeApp()
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())

    reply, history, products = asyncio.run(supervisor.ask(app, [], "I'd like to buy the Black Midi Dress, charge me for it."))

    assert "secure storefront checkout" in reply
    assert products == []
    assert app.graph_calls == 0
    assert all(agent.calls == 0 for agent in app.scout_specialists.values())
    assert history[-1] == {"role": "assistant", "content": reply}


def test_vague_shopping_clarification_avoids_models_and_tools(monkeypatch):
    app = FakeApp()
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())

    reply, _history, products = asyncio.run(supervisor.ask(app, [], "I need something nice for a party."))

    assert "what type of item" in reply.lower()
    assert "budget" in reply.lower()
    assert products == []
    assert app.graph_calls == 0
    assert all(agent.calls == 0 for agent in app.scout_specialists.values())


def test_out_of_scope_request_returns_scope_response_without_models_or_tools(monkeypatch):
    app = FakeApp()
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())

    reply, history, products = asyncio.run(supervisor.ask(app, [], "What is the weather today?"))

    assert "I don’t have live access" in reply
    assert "Scout products" in reply
    assert products == []
    assert app.graph_calls == 0
    assert all(agent.calls == 0 for agent in app.scout_specialists.values())
    assert history[-1] == {"role": "assistant", "content": reply}


def test_streaming_out_of_scope_request_returns_scope_response_without_models_or_tools(monkeypatch):
    app = FakeApp()
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())

    events = asyncio.run(_collect(supervisor.ask_streaming(app, [], "What is the weather today?")))

    assert events == [
        (
            "result",
            (
                "I don’t have live access for that kind of request here, but I can help with Scout products, "
                "availability, orders, checkout guidance, and return or refund policy.",
                [],
            ),
        )
    ]
    assert app.graph_calls == 0
    assert all(agent.calls == 0 for agent in app.scout_specialists.values())


def test_recommendation_evidence_complete_stops_before_final_model(monkeypatch):
    # A clear recommendation like this now correctly goes through a
    # separate, EVEN MORE deterministic tool-first path (see
    # tool_first.py) that bypasses the specialist agent's model
    # entirely - a stronger guarantee than what this test originally
    # checked. To preserve this test's real, original purpose (does the
    # system correctly avoid an unnecessary second model call once an
    # agent already has complete evidence), we disable that newer
    # optimization here specifically, so this test still exercises the
    # agent-level mechanism it was written for.
    from scout.agents.execution import tool_first as tool_first_module
    monkeypatch.setattr(tool_first_module, "_tool_first_call", lambda structured_intent, agent_name: (None, {}))

    app = FakeApp()
    product = {"product_id": "P001", "name": "Black Midi Dress", "price": 79.99, "source": "internal"}
    agent = EvidenceCompleteAgent("recommend_products", [product], agent_name="recommend_agent")
    app.scout_specialists["recommend_agent"] = agent
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())

    reply, _history, products = asyncio.run(supervisor.ask(app, [], "Recommend a dress under $80."))

    assert "Black Midi Dress" in reply
    assert products == [{**product, "image_url": "/static/products/P001.jpg"}]
    assert not agent.later_model_started


def test_policy_evidence_complete_renders_direct_approved_answer(monkeypatch):
    app = FakeApp()
    result = [
        {
            "statement": "Opened or worn items are not eligible for return unless defective.",
            "policy_name": "Return Policy",
            "source_document": "returns.md",
            "source_section": "returns",
        }
    ]
    agent = EvidenceCompleteAgent("retrieve_policy_chunks", result, agent_name="policy_agent")
    app.scout_specialists["policy_agent"] = agent
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())

    reply, _history, products = asyncio.run(supervisor.ask(app, [], "Can I return an opened item?"))

    assert "Opened or worn items usually aren’t eligible for a return unless they’re defective" in reply
    assert products == []
    assert not agent.later_model_started


def test_clear_recommendation_uses_deterministic_tool_first_not_the_agent(monkeypatch):
    # Renamed and rewritten: a clear recommendation now correctly bypasses
    # the specialist agent (and therefore the model) entirely, calling
    # recommend_products directly via the deterministic tool-first path
    # (see tool_first.py). This is a genuine, intentional improvement
    # found via repeated evaluation runs - the model would occasionally,
    # non-deterministically decline to call any tool for an unambiguous
    # recommendation request, producing a real, intermittent failure.
    app = FakeApp()
    captured = {}

    async def fake_tool(tool_name, args, *, agent_name):
        captured.update(tool_name=tool_name, args=args, agent_name=agent_name)
        return [{"product_id": "P001", "name": "Dress", "price": 79.99}]

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)

    reply, history, products = asyncio.run(supervisor.ask(app, [], "Recommend a dress under $80."))

    assert app.graph_calls == 0
    assert app.scout_specialists["recommend_agent"].calls == 0
    assert captured.get("tool_name") == "recommend_products"
    assert captured.get("agent_name") == "recommend_agent"
    assert "dress" in captured.get("args", {}).get("query", "").lower()
    assert history[-1]["role"] == "assistant"


def test_clear_size_inventory_request_invokes_inventory_agent_directly(monkeypatch):
    app = FakeApp()
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(reply="Inventory verified."))

    reply, history, products = asyncio.run(supervisor.ask(app, [], "Is the black midi dress in a medium?"))

    assert reply == "Inventory verified."
    assert products == []
    assert app.graph_calls == 0
    assert app.scout_specialists["inventory_agent"].calls == 1
    assert app.scout_specialists["recommend_agent"].calls == 0
    assert history[-1] == {"role": "assistant", "content": "Inventory verified."}


def test_uncertain_request_preserves_supervisor_graph(monkeypatch):
    app = FakeApp()
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "split_intents", lambda model, message: ["What about that one?"])
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized())

    reply, _history, _products = asyncio.run(supervisor.ask(app, [], "What about that one?"))

    assert reply == "Verified reply."
    assert app.graph_calls == 1
    assert all(agent.calls == 0 for agent in app.scout_specialists.values())


def test_external_marker_invokes_external_agent_once_and_marker_is_not_returned(monkeypatch):
    # This test specifically exercises the recommend_agent -> NEEDS_EXTERNAL_CHECK
    # -> external_offer_agent handoff via a fake agent - the deterministic
    # tool-first path (see tool_first.py) is disabled here so the query
    # correctly reaches that fake agent, rather than hitting the real,
    # seeded database (which genuinely has matches for this query) and
    # never reaching the handoff this test is designed to check.
    from scout.agents.execution import tool_first as tool_first_module
    monkeypatch.setattr(tool_first_module, "_tool_first_call", lambda structured_intent, agent_name: (None, {}))

    app = FakeApp()
    app.scout_specialists["recommend_agent"] = FakeAgent("recommend_agent", content="NEEDS_EXTERNAL_CHECK: red dress")
    app.scout_specialists["external_offer_agent"] = FakeAgent("external_offer_agent", content="External verified reply.")
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(reply=kwargs["original_reply"]))

    reply, _history, _products = asyncio.run(supervisor.ask(app, [], "Recommend a dress under $80."))

    assert reply == "External verified reply."
    assert "NEEDS_EXTERNAL_CHECK" not in reply
    assert app.scout_specialists["recommend_agent"].calls == 1
    assert app.scout_specialists["external_offer_agent"].calls == 1
    assert app.graph_calls == 0


def test_no_marker_does_not_invoke_external_agent(monkeypatch):
    # A clear recommendation with real internal matches now correctly
    # bypasses BOTH the recommend_agent specialist AND (since there's a
    # real match) the external_offer_agent entirely, via the
    # deterministic tool-first path (see tool_first.py).
    app = FakeApp()
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized())

    asyncio.run(supervisor.ask(app, [], "Recommend a dress under $80."))

    assert app.scout_specialists["recommend_agent"].calls == 0
    assert app.scout_specialists["external_offer_agent"].calls == 0


def test_direct_output_still_enters_verification_pipeline(monkeypatch):
    # Tests that a specialist's direct output still enters the
    # verification pipeline - the deterministic tool-first path (see
    # tool_first.py) is disabled here so this specific query reaches the
    # specialist agent, matching this test's real purpose.
    from scout.agents.execution import tool_first as tool_first_module
    monkeypatch.setattr(tool_first_module, "_tool_first_call", lambda structured_intent, agent_name: (None, {}))

    app = FakeApp()
    captured = {}
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())

    def capture_finalize(**kwargs):
        captured.update(kwargs)
        return finalized(reply="Pipeline reply.")

    monkeypatch.setattr(supervisor, "_finalize_verified_response", capture_finalize)

    reply, _history, _products = asyncio.run(supervisor.ask(app, [], "Recommend a dress under $80."))

    assert reply == "Pipeline reply."
    assert captured["original_reply"] == "Supported reply."
    assert captured["customer_message"] == "Recommend a dress under $80."


def test_direct_specialist_timeout_is_safe_and_cleans_context(monkeypatch):
    app = FakeApp()
    app.scout_specialists["recommend_agent"] = FakeAgent("recommend_agent", delay=10)
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor.settings, "SUB_INTENT_TIMEOUT_SECONDS", 0.01)
    monkeypatch.setattr(supervisor.settings, "CHAT_REQUEST_TIMEOUT_SECONDS", 0.5)

    reply, history, products = asyncio.run(supervisor.ask(app, [], "Recommend a dress under $80."))

    assert reply == SAFE_TIMEOUT_REPLY
    assert products == []
    assert history[-1] == {"role": "assistant", "content": SAFE_TIMEOUT_REPLY}
    assert get_diagnostics() is None


def test_streaming_and_non_streaming_use_same_direct_route(monkeypatch):
    # Both paths now consistently use the deterministic tool-first route
    # (see tool_first.py) for this clear recommendation, bypassing the
    # specialist agent entirely on both sides - genuinely the SAME
    # route, just a different one than before.
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized())
    non_stream = FakeApp()
    stream = FakeApp()

    asyncio.run(supervisor.ask(non_stream, [], "Recommend a dress under $80."))
    events = asyncio.run(_collect(supervisor.ask_streaming(stream, [], "Recommend a dress under $80.")))

    assert non_stream.graph_calls == 0
    assert stream.graph_calls == 0
    assert non_stream.scout_specialists["recommend_agent"].calls == 0
    assert stream.scout_specialists["recommend_agent"].calls == 0
    assert events[-1] == ("result", ("Verified reply.", []))


def test_api_and_sse_schemas_remain_unchanged():
    assert set(ChatRequest.model_fields) == {"message", "session_id"}
    assert set(ChatResponse.model_fields) == {"session_id", "reply", "products"}
    assert ChatResponse.model_fields["products"].default == []


async def _collect(async_iterable):
    return [item async for item in async_iterable]
