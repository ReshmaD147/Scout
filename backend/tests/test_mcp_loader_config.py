import asyncio
from pathlib import Path
from types import SimpleNamespace

from scout.agents import specialists, tools_loader


EXPECTED_ALLOWLISTS = {
    "recommend_agent": ["recommend_products", "search", "stock", "alternatives"],
    "inventory_agent": ["search", "stock", "stores", "fulfillment_options"],
    "order_agent": ["orders", "order_history", "return_eligibility"],
    "external_offer_agent": ["search_external_offers"],
    "policy_agent": ["retrieve_policy_chunks"],
}


class _DisabledStripeSettings:
    ENABLE_STRIPE_MCP = False

    @property
    def STRIPE_MCP_URL(self):
        raise AssertionError("disabled Stripe MCP path must not read STRIPE_MCP_URL")

    @property
    def STRIPE_MCP_KEY(self):
        raise AssertionError("disabled Stripe MCP path must not read STRIPE_MCP_KEY")


class _EnabledStripeSettings:
    ENABLE_STRIPE_MCP = True
    STRIPE_MCP_URL = "https://stripe.example/mcp"
    STRIPE_MCP_KEY = "test-placeholder-key"


class _FakeSession:
    async def __aenter__(self):
        return "scout-session"

    async def __aexit__(self, exc_type, exc, tb):
        return False


class _FakeExitStack:
    async def enter_async_context(self, context_manager):
        return await context_manager.__aenter__()

    async def aclose(self):
        return None


class _FakeMCPClient:
    instances = []

    def __init__(self, config):
        self.config = config
        self.get_tools_calls = []
        _FakeMCPClient.instances.append(self)

    def session(self, server_name):
        assert server_name == "scout"
        return _FakeSession()

    async def get_tools(self, server_name=None):
        self.get_tools_calls.append(server_name)
        assert server_name == "stripe"
        return [SimpleNamespace(name="stripe_create_payment_link")]


async def _fake_load_mcp_tools(session):
    assert session == "scout-session"
    return [SimpleNamespace(name="search"), SimpleNamespace(name="stock")]


def _run(coro):
    return asyncio.run(coro)


def test_disabled_stripe_mcp_configures_local_scout_only(monkeypatch):
    monkeypatch.setattr(tools_loader, "settings", _DisabledStripeSettings())

    config = tools_loader.build_mcp_servers_config()

    assert set(config) == {"scout"}
    assert config["scout"]["transport"] == "stdio"
    assert config["scout"]["command"] == "python3"


def test_disabled_stripe_mcp_returns_local_tools_without_stripe_discovery(monkeypatch):
    _FakeMCPClient.instances = []
    monkeypatch.setattr(tools_loader, "settings", _DisabledStripeSettings())
    monkeypatch.setattr(tools_loader, "MultiServerMCPClient", _FakeMCPClient)
    monkeypatch.setattr(tools_loader, "load_mcp_tools", _fake_load_mcp_tools)
    monkeypatch.setattr(tools_loader, "AsyncExitStack", _FakeExitStack)

    manager = tools_loader.MCPToolManager()
    tools = _run(manager.start())

    assert [tool.name for tool in tools] == ["search", "stock"]
    assert _FakeMCPClient.instances[0].get_tools_calls == []
    assert set(_FakeMCPClient.instances[0].config) == {"scout"}


def test_enabled_stripe_mcp_preserves_combined_loading_behavior(monkeypatch):
    _FakeMCPClient.instances = []
    monkeypatch.setattr(tools_loader, "settings", _EnabledStripeSettings())
    monkeypatch.setattr(tools_loader, "MultiServerMCPClient", _FakeMCPClient)
    monkeypatch.setattr(tools_loader, "load_mcp_tools", _fake_load_mcp_tools)
    monkeypatch.setattr(tools_loader, "AsyncExitStack", _FakeExitStack)

    manager = tools_loader.MCPToolManager()
    tools = _run(manager.start())

    assert [tool.name for tool in tools] == [
        "search",
        "stock",
        "stripe_create_payment_link",
    ]
    assert _FakeMCPClient.instances[0].get_tools_calls == ["stripe"]
    assert _FakeMCPClient.instances[0].config["stripe"] == {
        "transport": "streamable_http",
        "url": "https://stripe.example/mcp",
        "headers": {"Authorization": "Bearer test-placeholder-key"},
    }


def test_specialist_allowlists_remain_unchanged(monkeypatch):
    created_agents = {}

    def fake_create_specialist_agent(*, model, tools, name, prompt):
        agent = SimpleNamespace(name=name, tool_names=[tool.name for tool in tools])
        created_agents[name] = agent
        return agent

    all_tools = [
        SimpleNamespace(name=name)
        for name in {
            *{tool for tools in EXPECTED_ALLOWLISTS.values() for tool in tools},
            "stripe_create_payment_link",
            "stripe_refund_payment",
        }
    ]

    monkeypatch.setattr(specialists, "get_chat_model", lambda: object())
    monkeypatch.setattr(specialists, "wrap_tools_with_guard", lambda tools, agent_name=None: tools)
    monkeypatch.setattr(specialists, "create_specialist_agent", fake_create_specialist_agent)

    specialists.build_specialists(all_tools)

    assert {name: agent.tool_names for name, agent in created_agents.items()} == EXPECTED_ALLOWLISTS


def test_checkout_and_payment_service_sources_remain_deterministic():
    checkout_source = Path("src/scout/api/checkout.py").read_text()
    payment_source = Path("src/scout/services/payment_service.py").read_text()

    assert "from scout.services.payment_service import create_test_payment" in checkout_source
    assert "stripe.PaymentIntent.create" in payment_source
    assert "ENABLE_STRIPE_MCP" not in checkout_source
    assert "ENABLE_STRIPE_MCP" not in payment_source
