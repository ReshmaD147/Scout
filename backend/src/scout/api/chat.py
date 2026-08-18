import json
import uuid

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field, field_validator
from sse_starlette.sse import EventSourceResponse

from scout.agents.attribution import apply_attribution_and_cart_offer
from scout.agents.supervisor import ask, ask_streaming, SPECIALIST_NAMES
from scout.config import settings
from scout.services.chat_feedback_service import record_chat_feedback
from scout.services.recommendation_feedback_service import (
    list_recommendation_feedback,
    record_recommendation_feedback,
)
from scout.db.session import SessionLocal
from scout.services.order_service import (
    get_shipment_for_customer,
    list_orders_for_authenticated_customer,
)
from scout.repositories.product_repository import ProductRepository

router = APIRouter()

# In-memory session store: session_id -> message history.
# Dev-only simplification — resets on restart, doesn't scale across
# multiple server processes.
SESSION_HISTORIES: dict[str, list[dict]] = {}
SESSION_CONTEXTS: dict[str, dict] = {}


class ChatRequest(BaseModel):
    message: str
    session_id: str | None = None


class ChatResponse(BaseModel):
    session_id: str
    reply: str
    products: list[dict] = []


class ChatFeedbackRequest(BaseModel):
    session_id: str
    message_index: int
    rating: str
    user_message: str = ""
    assistant_reply: str = ""
    products: list[dict] = Field(default_factory=list)

    @field_validator("session_id")
    @classmethod
    def session_id_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("session_id is required")
        return value

    @field_validator("message_index")
    @classmethod
    def message_index_non_negative(cls, value: int) -> int:
        if value < 0:
            raise ValueError("message_index must be non-negative")
        return value

    @field_validator("rating")
    @classmethod
    def rating_supported(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"up", "down"}:
            raise ValueError("rating must be up or down")
        return normalized


class ChatFeedbackResponse(BaseModel):
    status: str
    feedback_id: str


class RecommendationFeedbackRequest(BaseModel):
    product_id: str
    rating: str
    session_id: str | None = None
    customer_id: str | None = None
    recommendation_id: str | None = None

    @field_validator("product_id")
    @classmethod
    def product_id_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("product_id is required")
        return value

    @field_validator("rating")
    @classmethod
    def recommendation_rating_supported(cls, value: str) -> str:
        normalized = value.strip().lower()
        if normalized not in {"up", "down"}:
            raise ValueError("rating must be up or down")
        return normalized


class RecommendationFeedbackResponse(BaseModel):
    status: str
    feedback_id: str
    rating: str


class DemoAuthRequest(BaseModel):
    customer_id: str
    session_id: str | None = None


class DemoAuthSignOutRequest(BaseModel):
    session_id: str | None = None


class DemoAuthResponse(BaseModel):
    session_id: str
    authenticated_customer_id: str | None = None
    demo_auth_enabled: bool


class AccountSummaryRequest(BaseModel):
    session_id: str

    @field_validator("session_id")
    @classmethod
    def account_session_id_required(cls, value: str) -> str:
        if not value.strip():
            raise ValueError("session_id is required")
        return value


@router.post("/demo-auth/sign-in", response_model=DemoAuthResponse)
async def demo_auth_sign_in(body: DemoAuthRequest) -> DemoAuthResponse:
    if not settings.demo_auth_enabled:
        raise HTTPException(status_code=404, detail="Demo auth is disabled.")
    customer_id = body.customer_id.strip()
    if customer_id not in settings.demo_customer_ids:
        raise HTTPException(status_code=400, detail="Unknown demo customer.")

    session_id = body.session_id or str(uuid.uuid4())
    context = SESSION_CONTEXTS.get(session_id, {})
    context["authenticated_customer_id"] = customer_id
    context["demo_authenticated"] = True
    SESSION_CONTEXTS[session_id] = context
    SESSION_HISTORIES.setdefault(session_id, [])
    return DemoAuthResponse(
        session_id=session_id,
        authenticated_customer_id=customer_id,
        demo_auth_enabled=True,
    )


@router.post("/demo-auth/sign-out", response_model=DemoAuthResponse)
async def demo_auth_sign_out(body: DemoAuthSignOutRequest) -> DemoAuthResponse:
    if not settings.demo_auth_enabled:
        raise HTTPException(status_code=404, detail="Demo auth is disabled.")
    session_id = body.session_id or str(uuid.uuid4())
    context = SESSION_CONTEXTS.get(session_id, {})
    context.pop("authenticated_customer_id", None)
    context.pop("demo_authenticated", None)
    SESSION_CONTEXTS[session_id] = context
    return DemoAuthResponse(
        session_id=session_id,
        authenticated_customer_id=None,
        demo_auth_enabled=True,
    )


@router.post("/account/summary")
async def account_summary(body: AccountSummaryRequest) -> dict:
    context = SESSION_CONTEXTS.get(body.session_id) or {}
    customer_id = context.get("authenticated_customer_id")
    if not customer_id:
        raise HTTPException(status_code=401, detail="Sign in to view account details.")

    session = SessionLocal()
    try:
        product_repo = ProductRepository(session)
        orders_result = list_orders_for_authenticated_customer(
            session,
            authenticated_customer_id=customer_id,
        )
        orders = orders_result.get("orders", [])
        for order in orders:
            shipment = get_shipment_for_customer(
                session,
                order["order_id"],
                authenticated_customer_id=customer_id,
            )
            order["shipment"] = shipment if shipment.get("authorized") else None
        latest_email = next(
            (order.get("contact_email") for order in orders if order.get("contact_email")),
            None,
        )
        feedback = list_recommendation_feedback(
            session_id=body.session_id,
            customer_id=customer_id,
        )
        for item in feedback:
            product = product_repo.get_by_id(item["product_id"])
            if product:
                item["product_name"] = product.name
    finally:
        session.close()

    return {
        "customer": {
            "customer_id": customer_id,
            "name": f"Demo Customer {customer_id[-1]}" if customer_id[-1:].isdigit() else "Demo Customer",
            "email": latest_email or f"{customer_id.lower()}@demo.lumi.local",
            "demo_identity": True,
        },
        "orders": orders,
        "recommendation_feedback": feedback,
    }


@router.post("/chat", response_model=ChatResponse)
async def chat(request: Request, body: ChatRequest) -> ChatResponse:
    session_id = body.session_id or str(uuid.uuid4())
    history = SESSION_HISTORIES.get(session_id, [])
    conversation_context = SESSION_CONTEXTS.get(session_id, {})

    supervisor_app = request.app.state.supervisor_app
    reply, updated_history, products = await ask(
        supervisor_app,
        history,
        body.message,
        conversation_context=conversation_context,
        session_id=session_id,
    )

    SESSION_HISTORIES[session_id] = updated_history
    SESSION_CONTEXTS[session_id] = conversation_context

    reply = apply_attribution_and_cart_offer(
        products=products,
        reply=reply,
        session_id=session_id,
        conversation_context=conversation_context,
    )
    SESSION_CONTEXTS[session_id] = conversation_context

    return ChatResponse(session_id=session_id, reply=reply, products=products)


@router.post("/chat/feedback", response_model=ChatFeedbackResponse)
async def chat_feedback(body: ChatFeedbackRequest) -> ChatFeedbackResponse:
    feedback_id = record_chat_feedback(
        session_id=body.session_id,
        message_index=body.message_index,
        rating=body.rating,
        user_message=body.user_message,
        assistant_reply=body.assistant_reply,
        products=body.products,
    )
    return ChatFeedbackResponse(status="ok", feedback_id=feedback_id)


@router.post("/recommendations/feedback", response_model=RecommendationFeedbackResponse)
async def recommendation_feedback(body: RecommendationFeedbackRequest) -> RecommendationFeedbackResponse:
    try:
        feedback = record_recommendation_feedback(
            product_id=body.product_id,
            rating=body.rating,
            session_id=body.session_id,
            customer_id=body.customer_id,
            recommendation_id=body.recommendation_id,
        )
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc
    return RecommendationFeedbackResponse(
        status="ok",
        feedback_id=feedback.feedback_id,
        rating=feedback.rating,
    )


@router.post("/chat/stream")
async def chat_stream(request: Request, body: ChatRequest):
    """Streams real progress events (understanding_request,
    searching_products, checking_inventory, verifying_claims,
    preparing_response) as the request genuinely moves through the
    supervisor/specialist graph, then emits the final, verification-gated
    reply in one 'done' event. No partial/unverified answer text is ever
    streamed — only real progress signals plus the finished, checked
    answer, matching the same verification pipeline the non-streaming
    /chat endpoint uses via ask()."""
    session_id = body.session_id or str(uuid.uuid4())
    history = SESSION_HISTORIES.get(session_id, [])
    conversation_context = SESSION_CONTEXTS.get(session_id, {})

    supervisor_app = request.app.state.supervisor_app

    async def event_generator():
        yield {"event": "session", "data": session_id}

        try:
            async for kind, payload in ask_streaming(
                supervisor_app,
                history,
                body.message,
                conversation_context=conversation_context,
                session_id=session_id,
            ):
                if kind == "progress":
                    yield {"event": "progress", "data": payload}
                else:
                    reply_text, products = payload
                    SESSION_HISTORIES[session_id] = history
                    cart_item = conversation_context.pop("completed_cart_add", None)
                    SESSION_CONTEXTS[session_id] = conversation_context
                    done_payload = {"reply": reply_text, "products": products}
                    if cart_item:
                        done_payload["cart_item"] = cart_item
                    yield {
                        "event": "done",
                        "data": json.dumps(done_payload),
                    }
        except Exception:
            yield {"event": "error", "data": "Something went wrong processing that request."}
            return

    return EventSourceResponse(event_generator())
