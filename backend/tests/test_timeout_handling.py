import asyncio
import json
from types import SimpleNamespace

from scout.agents import supervisor
from scout.agents.diagnostics import SAFE_TIMEOUT_REPLY, get_diagnostics
from scout.agents.evidence import get_tool_call_records
from scout.api import chat as chat_api
from scout.api.chat import ChatRequest, ChatResponse


class SlowApp:
    def __init__(self):
        self.calls = 0
        self.cancelled = False

    async def ainvoke(self, payload, config):
        self.calls += 1
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            self.cancelled = True
            raise


class SlowStreamingApp:
    def __init__(self):
        self.cancelled = False

    async def astream_events(self, payload, version, config):
        try:
            await asyncio.sleep(10)
        except asyncio.CancelledError:
            self.cancelled = True
            raise
        yield {"event": "on_chain_end", "name": "LangGraph", "metadata": {}, "data": {"output": {"messages": payload["messages"]}}}


class ProviderFailingAgent:
    async def astream_events(self, payload, version, config):
        class ReadTimeout(Exception):
            __module__ = "httpx"

        raise ReadTimeout("provider details must not leak")
        yield


class ProviderFailingDirectApp:
    def __init__(self):
        self.scout_specialists = {
            "recommend_agent": ProviderFailingAgent(),
            "inventory_agent": ProviderFailingAgent(),
            "order_agent": ProviderFailingAgent(),
            "external_offer_agent": ProviderFailingAgent(),
            "policy_agent": ProviderFailingAgent(),
        }


def _fast_settings(monkeypatch, *, model=0.02, sub_intent=0.05, chat=0.08):
    monkeypatch.setattr(supervisor.settings, "MODEL_INVOCATION_TIMEOUT_SECONDS", model)
    monkeypatch.setattr(supervisor.settings, "SUB_INTENT_TIMEOUT_SECONDS", sub_intent)
    monkeypatch.setattr(supervisor.settings, "CHAT_REQUEST_TIMEOUT_SECONDS", chat)
    monkeypatch.setattr(supervisor, "get_chat_model", lambda: object())


async def _collect(async_iterable):
    return [item async for item in async_iterable]


def test_model_call_timeout_returns_safe_response_and_cleans_context(monkeypatch):
    _fast_settings(monkeypatch, model=0.01)

    def slow_split(_model, _message):
        import time

        time.sleep(0.2)
        return ["dress"]

    monkeypatch.setattr(supervisor, "split_intents", slow_split)

    reply, history, products = asyncio.run(supervisor.ask(SlowApp(), [], "SECRET timeout text"))

    assert reply == SAFE_TIMEOUT_REPLY
    assert products == []
    assert "SECRET" not in reply
    assert history[-1] == {"role": "assistant", "content": SAFE_TIMEOUT_REPLY}
    assert get_tool_call_records() == []
    assert get_diagnostics() is None


def test_sub_intent_timeout_cancels_abandoned_graph_work(monkeypatch):
    _fast_settings(monkeypatch, sub_intent=0.01, chat=0.5)
    monkeypatch.setattr(supervisor, "split_intents", lambda _model, _message: ["dress"])
    app = SlowApp()

    reply, history, products = asyncio.run(supervisor.ask(app, [], "dress"))

    assert reply == SAFE_TIMEOUT_REPLY
    assert products == []
    assert app.cancelled
    assert app.calls == 1
    assert history[-1] == {"role": "assistant", "content": SAFE_TIMEOUT_REPLY}


def test_request_timeout_prevents_second_specialist_after_deadline(monkeypatch):
    _fast_settings(monkeypatch, sub_intent=1, chat=0.03)
    monkeypatch.setattr(supervisor, "split_intents", lambda _model, _message: ["dress", "order"])
    app = SlowApp()

    reply, history, products = asyncio.run(supervisor.ask(app, [], "dress and order"))

    assert reply == SAFE_TIMEOUT_REPLY
    assert products == []
    assert app.cancelled
    assert app.calls == 1
    assert [item["content"] for item in history].count("order") == 0


def test_no_correction_after_deadline(monkeypatch):
    _fast_settings(monkeypatch, sub_intent=1, chat=0.03)
    monkeypatch.setattr(supervisor, "split_intents", lambda _model, _message: ["dress"])
    monkeypatch.setattr(supervisor, "_attempt_targeted_correction", lambda *args, **kwargs: (_ for _ in ()).throw(AssertionError("correction must not run after timeout")))

    reply, _history, products = asyncio.run(supervisor.ask(SlowApp(), [], "dress"))

    assert reply == SAFE_TIMEOUT_REPLY
    assert products == []


def test_safe_non_streaming_timeout_response_from_api(monkeypatch):
    _fast_settings(monkeypatch, sub_intent=0.01, chat=0.5)
    monkeypatch.setattr(supervisor, "split_intents", lambda _model, _message: ["dress"])
    chat_api.SESSION_HISTORIES.clear()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(supervisor_app=SlowApp())))

    response = asyncio.run(chat_api.chat(request, ChatRequest(message="dress", session_id="s1")))

    assert response == ChatResponse(session_id="s1", reply=SAFE_TIMEOUT_REPLY, products=[])
    assert chat_api.SESSION_HISTORIES["s1"][-1]["content"] == SAFE_TIMEOUT_REPLY


def test_provider_transport_failure_returns_safe_http_200_schema(monkeypatch):
    _fast_settings(monkeypatch, sub_intent=1, chat=2)
    chat_api.SESSION_HISTORIES.clear()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(supervisor_app=ProviderFailingDirectApp())))

    response = asyncio.run(chat_api.chat(request, ChatRequest(message="Recommend a dress under $80.", session_id="provider-fail")))

    assert response == ChatResponse(session_id="provider-fail", reply=SAFE_TIMEOUT_REPLY, products=[])
    assert "provider details" not in response.reply


def test_safe_streaming_timeout_behavior_and_schema(monkeypatch):
    _fast_settings(monkeypatch, sub_intent=0.01, chat=0.5)
    monkeypatch.setattr(supervisor, "split_intents", lambda _model, _message: ["dress"])
    chat_api.SESSION_HISTORIES.clear()
    request = SimpleNamespace(app=SimpleNamespace(state=SimpleNamespace(supervisor_app=SlowStreamingApp())))

    async def collect_events():
        response = await chat_api.chat_stream(request, ChatRequest(message="dress", session_id="stream-timeout"))
        events = []
        async for event in response.body_iterator:
            events.append(event)
            if event.get("event") in {"done", "error"}:
                break
        return events

    events = asyncio.run(collect_events())

    assert events[0] == {"event": "session", "data": "stream-timeout"}
    assert events[-1]["event"] == "done"
    payload = json.loads(events[-1]["data"])
    assert payload == {"reply": SAFE_TIMEOUT_REPLY, "products": []}
    assert "timed out" not in events[-1]["data"]


def test_existing_api_and_sse_schemas_remain_unchanged():
    assert set(ChatRequest.model_fields) == {"message", "session_id"}
    assert set(ChatResponse.model_fields) == {"session_id", "reply", "products"}
    assert ChatResponse.model_fields["products"].default == []
