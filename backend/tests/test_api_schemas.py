from scout.api.chat import ChatRequest, ChatResponse


def test_chat_request_schema_fields_and_defaults():
    fields = ChatRequest.model_fields

    assert set(fields) == {"message", "session_id"}
    assert fields["message"].is_required()
    assert fields["session_id"].default is None


def test_chat_response_schema_fields_and_defaults():
    fields = ChatResponse.model_fields

    assert set(fields) == {"session_id", "reply", "products"}
    assert fields["session_id"].is_required()
    assert fields["reply"].is_required()
    assert fields["products"].default == []
