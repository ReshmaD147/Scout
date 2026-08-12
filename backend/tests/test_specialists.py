from types import SimpleNamespace

from langchain_core.language_models.fake_chat_models import FakeListChatModel
from langchain_core.tools import tool

from scout.agents import specialists


EXPECTED_ALLOWLISTS = {
    "recommend_agent": ["recommend_products", "search", "stock", "alternatives"],
    "inventory_agent": ["search", "stock", "stores", "fulfillment_options"],
    "order_agent": ["orders", "order_history", "return_eligibility"],
    "external_offer_agent": ["search_external_offers"],
    "policy_agent": ["retrieve_policy_chunks"],
}

FORBIDDEN_TOOL_FRAGMENTS = (
    "checkout",
    "payment",
    "pay",
    "refund",
    "cancel",
    "create_order",
    "update_order",
    "delete_order",
    "mutate",
    "inventory",
    "sql",
    "shell",
    "exec",
    "http",
    "request",
    "fetch",
)


def test_specialist_tool_allowlists(monkeypatch):
    created_agents = {}

    def fake_create_specialist_agent(*, model, tools, name, prompt):
        agent = SimpleNamespace(
            model=model,
            tools=tools,
            name=name,
            prompt=prompt,
            tool_names=[tool.name for tool in tools],
        )
        created_agents[name] = agent
        return agent

    all_tools = [
        SimpleNamespace(name=name)
        for name in {
            *{tool for tools in EXPECTED_ALLOWLISTS.values() for tool in tools},
            "checkout",
            "refund",
            "shell",
        }
    ]

    monkeypatch.setattr(specialists, "get_chat_model", lambda: object())
    monkeypatch.setattr(specialists, "wrap_tools_with_guard", lambda tools, agent_name=None: tools)
    monkeypatch.setattr(specialists, "create_specialist_agent", fake_create_specialist_agent)

    returned_agents = specialists.build_specialists(all_tools)

    assert set(returned_agents) == set(EXPECTED_ALLOWLISTS)
    assert {name: agent.tool_names for name, agent in created_agents.items()} == EXPECTED_ALLOWLISTS
    assert created_agents["order_agent"].tool_names == [
        "orders",
        "order_history",
        "return_eligibility",
    ]
    assert created_agents["external_offer_agent"].tool_names == ["search_external_offers"]


def test_effective_specialist_tool_lists_exclude_raw_stripe_and_unsafe_tools(monkeypatch):
    created_agents = {}

    def fake_create_specialist_agent(*, model, tools, name, prompt):
        agent = SimpleNamespace(name=name, tool_names=[tool.name for tool in tools])
        created_agents[name] = agent
        return agent

    raw_loaded_tools = [
        SimpleNamespace(name=name)
        for name in {
            *{tool for tools in EXPECTED_ALLOWLISTS.values() for tool in tools},
            "stripe_create_payment_link",
            "stripe_refund_payment",
            "checkout",
            "cancel_order",
            "sql_query",
            "shell_exec",
            "http_request",
        }
    ]

    monkeypatch.setattr(specialists, "get_chat_model", lambda: object())
    monkeypatch.setattr(specialists, "wrap_tools_with_guard", lambda tools, agent_name=None: tools)
    monkeypatch.setattr(specialists, "create_specialist_agent", fake_create_specialist_agent)

    specialists.build_specialists(raw_loaded_tools)

    effective_tool_names = {
        tool_name
        for agent in created_agents.values()
        for tool_name in agent.tool_names
    }
    assert {name: agent.tool_names for name, agent in created_agents.items()} == EXPECTED_ALLOWLISTS
    assert not any(
        fragment in tool_name
        for tool_name in effective_tool_names
        for fragment in FORBIDDEN_TOOL_FRAGMENTS
    )


def test_langchain_create_agent_signature_is_not_a_drop_in_prompt_swap():
    import inspect

    from langchain.agents import create_agent

    parameters = inspect.signature(create_agent).parameters

    assert "model" in parameters
    assert "tools" in parameters
    assert "name" in parameters
    assert "system_prompt" in parameters
    assert "prompt" not in parameters


def test_all_specialists_can_be_constructed_with_langchain_create_agent_adapter(monkeypatch):
    from langchain.agents import create_agent

    constructed_agents = {}

    def create_agent_adapter(*, model, tools, name, prompt):
        agent = create_agent(
            model=model,
            tools=tools,
            name=name,
            system_prompt=prompt,
        )
        constructed_agents[name] = agent
        return agent

    def make_tool(name):
        @tool(name)
        def fake_tool(query: str = "") -> str:
            """Deterministic test tool."""
            return "[]"

        return fake_tool

    all_tools = [
        make_tool(name)
        for name in {tool_name for tool_names in EXPECTED_ALLOWLISTS.values() for tool_name in tool_names}
    ]

    monkeypatch.setattr(specialists, "get_chat_model", lambda: FakeListChatModel(responses=["done"]))
    monkeypatch.setattr(specialists, "wrap_tools_with_guard", lambda tools, agent_name=None: tools)
    monkeypatch.setattr(specialists, "create_specialist_agent", create_agent_adapter)

    returned_agents = specialists.build_specialists(all_tools)

    assert set(returned_agents) == set(EXPECTED_ALLOWLISTS)
    assert set(constructed_agents) == set(EXPECTED_ALLOWLISTS)
    for agent_name, agent in constructed_agents.items():
        assert agent.name == agent_name
        assert hasattr(agent, "ainvoke")
        assert hasattr(agent, "astream_events")
        assert {"model", "tools"}.issubset(agent.nodes)
