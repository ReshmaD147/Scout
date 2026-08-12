import json

from fastapi import FastAPI
from fastapi.testclient import TestClient

from scout.api import chat as chat_api


def _test_app():
    app = FastAPI()
    app.state.supervisor_app = object()
    app.include_router(chat_api.router)
    return app


def _parse_sse_events(response_text):
    events = []
    current_event = {}
    data_lines = []

    for line in response_text.splitlines():
        if not line:
            if current_event or data_lines:
                if data_lines:
                    current_event["data"] = "\n".join(data_lines)
                events.append(current_event)
                current_event = {}
                data_lines = []
            continue
        if line.startswith("event: "):
            current_event["event"] = line.removeprefix("event: ")
        elif line.startswith("data: "):
            data_lines.append(line.removeprefix("data: "))

    if current_event or data_lines:
        if data_lines:
            current_event["data"] = "\n".join(data_lines)
        events.append(current_event)

    return events


def test_chat_stream_endpoint_emits_session_progress_and_done(monkeypatch):
    chat_api.SESSION_HISTORIES.clear()
    chat_api.SESSION_CONTEXTS.clear()

    async def fake_ask_streaming(app, history, message, conversation_context=None, debug=False):
        assert app is not None
        assert history == []
        assert message == "Recommend a dress under $80."
        assert conversation_context == {}
        yield ("progress", "searching_products")
        yield (
            "result",
            (
                "Black Midi Dress is a Scout option for $79.99.",
                [{"product_id": "P001", "name": "Black Midi Dress", "price": 79.99}],
            ),
        )

    monkeypatch.setattr(chat_api, "ask_streaming", fake_ask_streaming)

    with TestClient(_test_app()) as client:
        with client.stream(
            "POST",
            "/chat/stream",
            json={"message": "Recommend a dress under $80.", "session_id": "sess_sse"},
        ) as response:
            assert response.status_code == 200
            assert response.headers["content-type"].startswith("text/event-stream")
            events = _parse_sse_events(response.read().decode())

    assert events[0] == {"event": "session", "data": "sess_sse"}
    assert events[1] == {"event": "progress", "data": "searching_products"}
    assert events[2]["event"] == "done"
    done_payload = json.loads(events[2]["data"])
    assert done_payload == {
        "reply": "Black Midi Dress is a Scout option for $79.99.",
        "products": [{"product_id": "P001", "name": "Black Midi Dress", "price": 79.99}],
    }
    assert "sess_sse" in chat_api.SESSION_HISTORIES
    assert "sess_sse" in chat_api.SESSION_CONTEXTS


def test_chat_stream_endpoint_masks_raw_stream_exceptions(monkeypatch):
    async def exploding_ask_streaming(*args, **kwargs):
        if False:
            yield None
        raise RuntimeError("SECRET api_key=abc traceback")

    monkeypatch.setattr(chat_api, "ask_streaming", exploding_ask_streaming)

    with TestClient(_test_app()) as client:
        response = client.post("/chat/stream", json={"message": "hello", "session_id": "sess_error"})

    events = _parse_sse_events(response.text)

    assert response.status_code == 200
    assert events[0] == {"event": "session", "data": "sess_error"}
    assert events[1] == {"event": "error", "data": "Something went wrong processing that request."}
    assert "SECRET" not in response.text
    assert "api_key" not in response.text
    assert "traceback" not in response.text
