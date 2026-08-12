import asyncio
from types import SimpleNamespace

from scout.agents import supervisor
from scout.agents.intent_splitter import StructuredIntent
from scout.agents.orchestration.turn_context import TurnExecutionContext
from scout.agents.orchestration.turn_executor import execute_single_intent_turn


def ai(name, content):
    return SimpleNamespace(name=name, content=content, type="ai", tool_calls=None)


class FakeAgent:
    def __init__(self, name, *, delay=0):
        self.name = name
        self.delay = delay
        self.calls = []

    async def ainvoke(self, payload, config):
        self.calls.append(payload)
        if self.delay:
            await asyncio.sleep(self.delay)
        return {"messages": [*payload["messages"], ai(self.name, f"{self.name} reply.")]}


class FakeApp:
    def __init__(self):
        self.scout_specialists = {
            "recommend_agent": FakeAgent("recommend_agent"),
            "inventory_agent": FakeAgent("inventory_agent"),
            "order_agent": FakeAgent("order_agent"),
            "external_offer_agent": FakeAgent("external_offer_agent"),
            "policy_agent": FakeAgent("policy_agent"),
        }

    async def ainvoke(self, payload, config):
        return {"messages": [*payload["messages"], ai("policy_agent", "Graph reply.")]}


def finalized(reply="Verified reply.", products=None):
    return supervisor.FinalizedResponse(
        reply=reply,
        products=products or [],
        proposed_claims=[],
        verification_result=SimpleNamespace(
            verified=True,
            approved_claim_ids=[],
            rejected_claims=[],
            correction_agent=None,
        ),
    )


async def no_correction(*args, **kwargs):
    return kwargs["initial"]


async def collect_result(app, message, context):
    result = None
    async for kind, payload in execute_single_intent_turn(
        app,
        [],
        message,
        conversation_context={},
        turn_context=context,
    ):
        if kind == "result":
            result = payload
    return result


async def collect_concurrently(*coroutines):
    return await asyncio.gather(*coroutines)


def test_concurrent_turns_route_to_different_specialists_without_app_state(monkeypatch):
    app = FakeApp()
    tool_calls = []

    async def fake_tool(tool_name, args, *, agent_name):
        tool_calls.append((tool_name, dict(args), agent_name))
        await asyncio.sleep(0.01)
        return {"available": False}

    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(reply=kwargs["original_reply"]))
    monkeypatch.setattr(supervisor, "_attempt_targeted_correction", no_correction)

    inventory_context = TurnExecutionContext(
        direct_specialist="inventory_agent",
        structured_intent=StructuredIntent(
            text="Is the Black Midi Dress available in Black, size M?",
            request_type="inventory_availability",
            confidence=0.9,
            product_id="P001",
            color="Black",
            size="M",
        ),
    )
    policy_context = TurnExecutionContext(
        direct_specialist="policy_agent",
        structured_intent=StructuredIntent(
            text="What is your return policy?",
            request_type="policy_question",
            confidence=0.9,
        ),
    )

    inventory_result, policy_result = asyncio.run(
        collect_concurrently(
            collect_result(app, "Is the Black Midi Dress available in Black, size M?", inventory_context),
            collect_result(app, "What is your return policy?", policy_context),
        )
    )

    assert inventory_result is not None
    assert policy_result is not None
    assert app.scout_specialists["policy_agent"].calls
    assert ("stock", {"product_id": "P001", "size": "M", "color": "Black"}, "inventory_agent") in tool_calls
    assert not hasattr(app, "_scout_direct_specialist")
    assert not hasattr(app, "_scout_structured_intent")


def test_concurrent_structured_intents_do_not_cross_requests(monkeypatch):
    from scout.agents.orchestration import turn_executor

    app = FakeApp()
    seen = []

    async def fake_invoke(agent, payload, config, *, agent_name, structured_intent=None):
        await asyncio.sleep(0.01 if "return" in structured_intent.text else 0)
        seen.append((agent_name, structured_intent.text, structured_intent.request_type))
        return {"messages": [*payload["messages"], ai(agent_name, f"{agent_name} reply.")]}

    monkeypatch.setattr(turn_executor, "_invoke_agent_with_timing", fake_invoke)
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(reply=kwargs["original_reply"]))
    monkeypatch.setattr(supervisor, "_attempt_targeted_correction", no_correction)

    return_context = TurnExecutionContext(
        direct_specialist="policy_agent",
        structured_intent=StructuredIntent(
            text="What is your return policy?",
            request_type="policy_question",
            confidence=0.9,
        ),
    )
    shipping_context = TurnExecutionContext(
        direct_specialist="policy_agent",
        structured_intent=StructuredIntent(
            text="What is your shipping policy?",
            request_type="policy_question",
            confidence=0.9,
        ),
    )

    asyncio.run(
        collect_concurrently(
            collect_result(app, "What is your return policy?", return_context),
            collect_result(app, "What is your shipping policy?", shipping_context),
        )
    )

    assert ("policy_agent", "What is your return policy?", "policy_question") in seen
    assert ("policy_agent", "What is your shipping policy?", "policy_question") in seen
    assert len(seen) == 2


def test_concurrent_inventory_variant_args_stay_with_their_request(monkeypatch):
    app = FakeApp()
    calls = []

    async def fake_tool(tool_name, args, *, agent_name):
        await asyncio.sleep(0.01 if args.get("size") == "M" else 0)
        calls.append((tool_name, dict(args), agent_name))
        return {"available": args.get("size") == "S"}

    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(reply=kwargs["original_reply"]))
    monkeypatch.setattr(supervisor, "_attempt_targeted_correction", no_correction)

    medium_context = TurnExecutionContext(
        direct_specialist="inventory_agent",
        structured_intent=StructuredIntent(
            text="Is the Black Midi Dress available in Black, size M?",
            request_type="inventory_availability",
            confidence=0.9,
            product_id="P001",
            color="Black",
            size="M",
        ),
    )
    small_context = TurnExecutionContext(
        direct_specialist="inventory_agent",
        structured_intent=StructuredIntent(
            text="Is the Black Midi Dress available in Black, size S?",
            request_type="inventory_availability",
            confidence=0.9,
            product_id="P001",
            color="Black",
            size="S",
        ),
    )

    asyncio.run(
        collect_concurrently(
            collect_result(app, "Is the Black Midi Dress available in Black, size M?", medium_context),
            collect_result(app, "Is the Black Midi Dress available in Black, size S?", small_context),
        )
    )

    assert ("stock", {"product_id": "P001", "size": "M", "color": "Black"}, "inventory_agent") in calls
    assert ("stock", {"product_id": "P001", "size": "S", "color": "Black"}, "inventory_agent") in calls


def test_compiled_app_has_no_request_routing_attributes_after_ask(monkeypatch):
    app = FakeApp()

    async def fake_tool(tool_name, args, *, agent_name):
        return {"available": False}

    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())
    monkeypatch.setattr(supervisor, "_execute_read_only_tool", fake_tool)
    monkeypatch.setattr(supervisor, "_finalize_verified_response", lambda **kwargs: finalized(reply=kwargs["original_reply"]))
    monkeypatch.setattr(supervisor, "_attempt_targeted_correction", no_correction)

    asyncio.run(supervisor.ask(app, [], "Is the black midi dress in a medium?"))

    assert not hasattr(app, "_scout_direct_specialist")
    assert not hasattr(app, "_scout_structured_intent")
