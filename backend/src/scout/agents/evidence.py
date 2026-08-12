from __future__ import annotations

import contextvars
import json
from copy import deepcopy
from datetime import datetime, timezone
from math import isfinite
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, ConfigDict, Field, field_validator

from scout.agents.diagnostics import get_diagnostics

# ─────────────────────────────────────────────────────────
# EVIDENCE / CLAIMS DATA MODELS
# This module is the foundation of the verification pipeline: every
# tool call gets recorded as evidence, structured claims are proposed
# from that evidence, and verification checks each claim against its
# linked evidence before anything reaches the customer. Everything else
# in the agent layer (claims.py, verification.py, rendering.py) builds
# on top of the types and functions defined here.
# ─────────────────────────────────────────────────────────

JsonCompatible = str | int | float | bool | None | list["JsonCompatible"] | dict[str, "JsonCompatible"]
MAX_UNWRAP_DEPTH = 8
MAX_UNWRAP_LIST_ITEMS = 100
MAX_UNWRAP_STRING_CHARS = 200_000
ENVELOPE_KEYS = {
    "content",
    "artifact",
    "text",
    "data",
    "result",
    "structuredContent",
    "structured_content",
}


def make_evidence_id() -> str:
    """Generate a unique evidence entry ID."""
    return f"ev_{uuid4().hex}"


def make_tool_call_id() -> str:
    """Generate a unique tool call record ID."""
    return f"tc_{uuid4().hex}"


def make_claim_id() -> str:
    """Generate a unique proposed claim ID."""
    return f"cl_{uuid4().hex}"


def make_sub_intent_id() -> str:
    """Generate a unique sub-intent ID."""
    return f"sub_{uuid4().hex}"


def utc_now() -> datetime:
    """Current UTC time, timezone-aware."""
    return datetime.now(timezone.utc)


def is_json_compatible(value: Any) -> bool:
    """True if value can be safely stored/serialized as JSON — used to
    validate Pydantic fields that must stay JSON-safe throughout the
    evidence/claims pipeline.
    """
    if value is None or isinstance(value, str | int | bool):
        return True

    if isinstance(value, float):
        return isfinite(value)

    if isinstance(value, bytes):
        return False

    if isinstance(value, list):
        return all(is_json_compatible(item) for item in value)

    if isinstance(value, dict):
        return all(
            isinstance(key, str) and is_json_compatible(item)
            for key, item in value.items()
        )

    return False


def require_json_compatible(value: Any, field_name: str) -> Any:
    """Validator helper: raise if value isn't JSON-compatible."""
    if not is_json_compatible(value):
        raise ValueError(f"{field_name} must contain only JSON-compatible values")
    return value


def _not_blank(value: str, field_name: str) -> str:
    """Validator helper: raise if a required string is empty/whitespace."""
    if not value or not value.strip():
        raise ValueError(f"{field_name} must not be blank")
    return value


def _timezone_aware(value: datetime, field_name: str) -> datetime:
    """Validator helper: raise if a datetime lacks timezone info."""
    if value.tzinfo is None or value.utcoffset() is None:
        raise ValueError(f"{field_name} must be timezone-aware")
    return value


class EvidenceEntry(BaseModel):
    """One piece of normalized, structured data produced by a
    successful tool call — the actual factual basis that proposed
    claims must be traceable back to.
    """
    model_config = ConfigDict(extra="forbid")

    evidence_id: str = Field(default_factory=make_evidence_id)
    sequence: int = Field(ge=0)
    sub_intent_id: str
    attempt_number: int = Field(ge=0)
    agent_name: str
    tool_name: str
    original_args: dict[str, Any] = Field(default_factory=dict)
    validated_args: dict[str, Any] = Field(default_factory=dict)
    entity_type: str | None = None
    entity_id: str | None = None
    normalized_facts: dict[str, Any] = Field(default_factory=dict)
    success: bool
    error_code: str | None = None
    timestamp: datetime = Field(default_factory=utc_now)

    @field_validator("evidence_id", "sub_intent_id", "agent_name", "tool_name")
    @classmethod
    def required_strings_are_not_blank(cls, value: str, info):
        return _not_blank(value, info.field_name)

    @field_validator("original_args", "validated_args", "normalized_facts")
    @classmethod
    def dicts_are_json_compatible(cls, value: dict[str, Any], info):
        return require_json_compatible(value, info.field_name)

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_timezone_aware(cls, value: datetime, info):
        return _timezone_aware(value, info.field_name)


class ToolCallRecord(BaseModel):
    """Log of one tool call attempt — successful or not. Distinct from
    EvidenceEntry: every tool call gets a ToolCallRecord, but only
    successful calls that produce usable normalized facts also get a
    linked EvidenceEntry (evidence_id is None otherwise).
    """
    model_config = ConfigDict(extra="forbid")

    call_id: str = Field(default_factory=make_tool_call_id)
    sequence: int = Field(ge=0)
    sub_intent_id: str
    attempt_number: int = Field(ge=0)
    agent_name: str
    tool_name: str
    original_args: dict[str, Any] = Field(default_factory=dict)
    validated_args: dict[str, Any] = Field(default_factory=dict)
    success: bool
    evidence_id: str | None = None
    error_code: str | None = None
    timestamp: datetime = Field(default_factory=utc_now)

    @field_validator("call_id", "sub_intent_id", "agent_name", "tool_name")
    @classmethod
    def required_strings_are_not_blank(cls, value: str, info):
        return _not_blank(value, info.field_name)

    @field_validator("original_args", "validated_args")
    @classmethod
    def args_are_json_compatible(cls, value: dict[str, Any], info):
        return require_json_compatible(value, info.field_name)

    @field_validator("evidence_id")
    @classmethod
    def optional_evidence_id_is_not_blank(cls, value: str | None, info):
        if value is not None:
            return _not_blank(value, info.field_name)
        return value

    @field_validator("timestamp")
    @classmethod
    def timestamp_is_timezone_aware(cls, value: datetime, info):
        return _timezone_aware(value, info.field_name)


class ProposedClaim(BaseModel):
    """A single factual statement the model wants to make (e.g. "product
    P002 costs $53.12"), proposed for verification against linked
    evidence before it's allowed into a customer-facing reply.
    """
    model_config = ConfigDict(extra="forbid")

    claim_id: str = Field(default_factory=make_claim_id)
    claim_type: str
    subject_id: str | None = None
    field: str
    value: Any
    evidence_ids: list[str] = Field(default_factory=list)
    source_agent: str

    @field_validator("claim_id", "claim_type", "field", "source_agent")
    @classmethod
    def required_strings_are_not_blank(cls, value: str, info):
        return _not_blank(value, info.field_name)

    @field_validator("value")
    @classmethod
    def value_is_json_compatible(cls, value: Any, info):
        return require_json_compatible(value, info.field_name)

    @field_validator("evidence_ids")
    @classmethod
    def evidence_ids_are_not_blank(cls, value: list[str]):
        for evidence_id in value:
            _not_blank(evidence_id, "evidence_ids")
        return value


class RejectedClaim(BaseModel):
    """A proposed claim that failed verification, with a reason code
    explaining why — used both for internal diagnostics and to decide
    which specialist should attempt the one bounded correction.
    """
    model_config = ConfigDict(extra="forbid")

    claim_id: str
    reason_code: str
    explanation: str
    responsible_agent: str | None = None
    missing_evidence: list[str] = Field(default_factory=list)

    @field_validator("claim_id", "reason_code", "explanation")
    @classmethod
    def required_strings_are_not_blank(cls, value: str, info):
        return _not_blank(value, info.field_name)

    @field_validator("missing_evidence")
    @classmethod
    def missing_evidence_ids_are_not_blank(cls, value: list[str]):
        for evidence_id in value:
            _not_blank(evidence_id, "missing_evidence")
        return value


class VerificationResult(BaseModel):
    """Outcome of checking a set of proposed claims against evidence —
    which claims were approved, which were rejected and why, and which
    single agent (if any) should get the one bounded correction attempt.
    """
    model_config = ConfigDict(extra="forbid")

    verified: bool
    approved_claim_ids: list[str] = Field(default_factory=list)
    rejected_claims: list[RejectedClaim] = Field(default_factory=list)
    missing_evidence: list[str] = Field(default_factory=list)
    correction_agent: str | None = None

    @field_validator("approved_claim_ids", "missing_evidence")
    @classmethod
    def id_lists_are_not_blank(cls, value: list[str], info):
        for item in value:
            _not_blank(item, info.field_name)
        return value


# ─────────────────────────────────────────────────────────
# CONSTANTS
# ─────────────────────────────────────────────────────────

SENSITIVE_KEY_PARTS = (
    "api_key",
    "token",
    "secret",
    "password",
    "authorization",
    "credential",
)

# Every key a tool result is allowed to surface into normalized facts.
# Anything not in this set gets dropped during normalization unless
# it's a nested dict/list containing keys that ARE in this set — this
# is the actual mechanism that keeps evidence data lean and prevents
# unexpected/unvetted fields from silently entering the pipeline.
FACT_KEYS = {
    "product_id",
    "external_product_id",
    "order_id",
    "store_id",
    "customer_id",
    "name",
    "product_name",
    "brand",
    "vendor_name",
    "price",
    "discounted_price",
    "promotion",
    "quantity",
    "total_quantity",
    "available",
    "in_stock",
    "size",
    "color",
    "variants",
    "found",
    "status",
    "tracking_number",
    "payment_status",
    "return_eligible",
    "likely_eligible",
    "eligible",
    "return_window_days",
    "reason",
    "click_url",
    "category",
    "subcategory",
    "use_cases",
    "attributes",
    "waterproof",
    "availability",
    "product_url",
    "last_verified_at",
    "currency",
    "original_price",
    "satisfied_constraints",
    "matches",
    "items",
    "match_count",
    "candidate_count",
    "unmet_constraints",
    "filters",
    "message",
    "department",
    "rating",
    "score",
    "pickup",
    "stores",
    "store_name",
    "address",
    "distance_miles",
    "requested_store_had_no_stock",
    "pickup_available",
    "delivery_available",
    "pickup_estimate",
    "delivery_estimate",
    "shipping",
    "fulfillment_options",
    "source",
    "source_file",
    "document",
    "metadata",
    "title",
    "chunk_id",
    "statement",
    "policy_name",
    "policy_version",
    "source_document",
    "source_section",
}

ERROR_TOOL_CALL_LIMIT_EXCEEDED = "tool_call_limit_exceeded"
ERROR_REPEATED_TOOL_CALL = "repeated_tool_call"
ERROR_ARGUMENT_VALIDATION_FAILED = "argument_validation_failed"
ERROR_TOOL_EXECUTION_FAILED = "tool_execution_failed"
ERROR_EVIDENCE_NORMALIZATION_FAILED = "evidence_normalization_failed"


# ─────────────────────────────────────────────────────────
# EVIDENCE CONTEXT
# Per-sub-intent state, held in a contextvar. Tool calls during a
# sub-intent accumulate here; claims/verification later read from it.
#
# NOTE: this context is scoped by sub_intent_id, NOT by which specialist
# is currently running. If more than one specialist runs tool calls
# within the same sub-intent (e.g. a NEEDS_EXTERNAL_CHECK handoff),
# their evidence entries all land in this same shared list — only
# EvidenceEntry.agent_name distinguishes which entry came from which
# agent, after the fact. Anything reading get_evidence_entries() gets
# every agent's evidence for this sub-intent, unfiltered. This is a
# known, deliberate architectural tradeoff (see CLAUDE.md), not a bug.
# ─────────────────────────────────────────────────────────

class _EvidenceContext(BaseModel):
    """Internal, mutable per-sub-intent state — tool call records and
    evidence entries accumulate here as an agent runs.
    """
    model_config = ConfigDict(arbitrary_types_allowed=True)

    sub_intent_id: str
    attempt_number: int = Field(ge=0)
    current_agent: str | None = None
    next_sequence: int = 0
    tool_call_records: list[ToolCallRecord] = Field(default_factory=list)
    evidence_entries: list[EvidenceEntry] = Field(default_factory=list)


_active_evidence_context: contextvars.ContextVar[_EvidenceContext | None] = (
    contextvars.ContextVar("active_evidence_context", default=None)
)


def start_evidence_context(sub_intent_id: str, attempt_number: int = 0) -> None:
    """Activate evidence tracking for a sub-intent. If the same
    sub_intent_id is already active (e.g. calling again for a
    correction attempt), updates the attempt_number in place rather
    than starting a fresh context — so evidence from earlier attempts
    of the SAME sub-intent stays available.
    """
    _not_blank(sub_intent_id, "sub_intent_id")
    if attempt_number < 0:
        raise ValueError("attempt_number must be non-negative")

    current = _active_evidence_context.get()
    if current and current.sub_intent_id == sub_intent_id:
        current.attempt_number = attempt_number
        current.current_agent = None
        return

    _active_evidence_context.set(
        _EvidenceContext(sub_intent_id=sub_intent_id, attempt_number=attempt_number)
    )


def set_current_agent(agent_name: str) -> None:
    """Record which specialist is currently making tool calls — used
    to tag new evidence/tool-call records with the right agent_name.
    """
    _not_blank(agent_name, "agent_name")
    context = _active_evidence_context.get()
    if context is not None:
        context.current_agent = agent_name


def get_tool_call_records() -> list[ToolCallRecord]:
    """Return a deep copy of every tool call record for the active
    context — copies are returned so callers can't accidentally mutate
    the live, in-progress context.
    """
    context = _active_evidence_context.get()
    if context is None:
        return []
    return [record.model_copy(deep=True) for record in context.tool_call_records]


def get_evidence_entries() -> list[EvidenceEntry]:
    """Return a deep copy of every evidence entry for the active
    context (see module note above on how this is scoped).
    """
    context = _active_evidence_context.get()
    if context is None:
        return []
    return [entry.model_copy(deep=True) for entry in context.evidence_entries]


def clear_evidence_context() -> None:
    """Deactivate evidence tracking. Call once a sub-intent is fully
    finalized, so its evidence never leaks into a later, unrelated one.
    """
    _active_evidence_context.set(None)


# ─────────────────────────────────────────────────────────
# SANITIZATION HELPERS
# ─────────────────────────────────────────────────────────

def _is_sensitive_key(key: str) -> bool:
    """True if a key looks like it might hold a secret (api_key, token,
    password, etc.) — used to redact rather than store such values.
    """
    lower_key = key.lower()
    return any(part in lower_key for part in SENSITIVE_KEY_PARTS)


def _safe_json_value(value: Any, *, redact_sensitive_keys: bool = False) -> Any:
    """Recursively strip a value down to something guaranteed
    JSON-safe: non-finite floats and bytes become None (then get
    dropped from lists/dicts), non-string dict keys are dropped, and —
    if requested — sensitive-looking keys are redacted rather than
    stored. This is the actual sanitization boundary tool arguments and
    results pass through before entering the evidence pipeline.
    """
    if value is None or isinstance(value, str | int | bool):
        return value

    if isinstance(value, float):
        return value if isfinite(value) else None

    if isinstance(value, bytes):
        return None

    if isinstance(value, list | tuple):
        items = [
            _safe_json_value(item, redact_sensitive_keys=redact_sensitive_keys)
            for item in value
        ]
        return [item for item in items if item is not None]

    if isinstance(value, dict):
        normalized = {}
        for key, item in value.items():
            if not isinstance(key, str):
                continue
            if redact_sensitive_keys and _is_sensitive_key(key):
                normalized[key] = "[REDACTED]"
                continue
            safe_item = _safe_json_value(item, redact_sensitive_keys=redact_sensitive_keys)
            if safe_item is not None:
                normalized[key] = safe_item
        return normalized

    return None


def normalize_args(args: Any) -> dict[str, Any]:
    """Sanitize a tool call's arguments for safe storage — redacts
    anything that looks like a credential, drops non-JSON-safe values.
    Returns {} for anything that isn't a dict to begin with.
    """
    if not isinstance(args, dict):
        return {}
    normalized = _safe_json_value(args, redact_sensitive_keys=True)
    return normalized if isinstance(normalized, dict) else {}


def _normalize_fact_mapping(value: dict[str, Any]) -> dict[str, Any]:
    """Recursively filter a raw dict down to only FACT_KEYS-allowed
    keys (at any nesting level), dropping sensitive keys entirely.
    This is the actual gate that decides which fields from a tool's raw
    result survive into normalized evidence — confirmed via direct
    testing tonight to correctly preserve nested lists of dicts (e.g.
    a "stores" list containing store_name/address/quantity) as long as
    the outer key itself is in FACT_KEYS.
    """
    normalized = {}
    for key, item in value.items():
        if not isinstance(key, str) or _is_sensitive_key(key):
            continue

        if key in FACT_KEYS:
            safe_item = _safe_json_value(item)
            if safe_item is not None:
                normalized[key] = safe_item
            continue

        if isinstance(item, dict):
            nested = _normalize_fact_mapping(item)
            if nested:
                normalized[key] = nested
        elif isinstance(item, list):
            nested_items = []
            for nested_item in item:
                if isinstance(nested_item, dict):
                    nested = _normalize_fact_mapping(nested_item)
                    if nested:
                        nested_items.append(nested)
                else:
                    safe_item = _safe_json_value(nested_item)
                    if safe_item is not None and key in FACT_KEYS:
                        nested_items.append(safe_item)
            if nested_items and key in FACT_KEYS:
                normalized[key] = nested_items

    return normalized


# ─────────────────────────────────────────────────────────
# TOOL RESULT UNWRAPPING
# Real tool results arrive in several different shapes depending on
# context: a plain dict (calling a tool function directly), or a
# LangChain/MCP-wrapped (content, artifact) tuple with the actual
# payload JSON-encoded as a string inside a {'type': 'text', 'text':
# '...'} block (the shape seen when a specialist calls a tool through
# the full agent pipeline). This section recursively unwraps whichever
# shape shows up into a flat list of plain dicts/lists.
# ─────────────────────────────────────────────────────────

def unwrap_tool_result_payload(*, content: Any, artifact: Any = None) -> list[dict | list]:
    """Entry point for unwrapping a raw tool result into a list of
    plain dict/list payloads, regardless of what shape it originally
    arrived in.
    """
    payloads: list[dict | list] = []
    _unwrap_payload(content, payloads, depth=0)
    if artifact is not None:
        _unwrap_payload(artifact, payloads, depth=0)
    return [deepcopy(payload) for payload in payloads]


def _unwrap_payload(value: Any, payloads: list[dict | list], *, depth: int) -> None:
    """Recursively unwrap one value, appending any plain dict/list
    payloads found along the way to `payloads`. Handles: LangChain
    ToolMessage/MCP objects (via _safe_tool_object_mapping), JSON-encoded
    strings, plain dicts (recursing into ENVELOPE_KEYS to find nested
    payloads), plain lists, and (content, artifact) tuples. Stops
    recursing past MAX_UNWRAP_DEPTH to avoid unbounded recursion on
    unexpected/malicious input shapes.
    """
    if depth > MAX_UNWRAP_DEPTH or value is None:
        return

    safe_object = _safe_tool_object_mapping(value)
    if safe_object is not None:
        _unwrap_payload(safe_object, payloads, depth=depth + 1)
        return

    if isinstance(value, str):
        if len(value) > MAX_UNWRAP_STRING_CHARS:
            return
        try:
            parsed = json.loads(value)
        except (TypeError, json.JSONDecodeError):
            return
        _unwrap_payload(parsed, payloads, depth=depth + 1)
        return

    if isinstance(value, dict):
        safe_mapping = _safe_json_value(value)
        if isinstance(safe_mapping, dict):
            payloads.append(safe_mapping)
        for key in ENVELOPE_KEYS:
            if key in value:
                _unwrap_payload(value[key], payloads, depth=depth + 1)
        return

    if isinstance(value, list):
        safe_items = []
        for item in value[:MAX_UNWRAP_LIST_ITEMS]:
            if isinstance(item, dict) and not _is_envelope_mapping(item):
                safe_item = _safe_json_value(item)
                if safe_item is not None:
                    safe_items.append(safe_item)
                continue
            _unwrap_payload(item, payloads, depth=depth + 1)
        if safe_items:
            payloads.append(safe_items)
        return

    if isinstance(value, tuple) and len(value) == 2:
        content, artifact = value
        if artifact is not None:
            _unwrap_payload(artifact, payloads, depth=depth + 1)
        _unwrap_payload(content, payloads, depth=depth + 1)


def _is_envelope_mapping(value: dict[str, Any]) -> bool:
    """True if a dict looks like a wrapper/envelope (has an
    ENVELOPE_KEYS key, or type in {"text", "json"}) rather than a plain
    data payload — used to decide whether to unwrap further or treat
    it as a leaf value.
    """
    return any(key in value for key in ENVELOPE_KEYS) or value.get("type") in {"text", "json"}


def _safe_tool_object_mapping(value: Any) -> dict[str, Any] | None:
    """Convert known non-plain object types (LangChain ToolMessage, MCP
    Pydantic models) into a plain dict for further unwrapping. Returns
    None for plain JSON-safe types or unrecognized objects — those fall
    through to the other branches in _unwrap_payload, or are simply
    dropped if nothing recognizes them.
    """
    if isinstance(value, dict | list | str | int | float | bool) or value is None:
        return None
    class_name = value.__class__.__name__
    module_name = value.__class__.__module__
    if class_name == "ToolMessage":
        return {
            key: getattr(value, key)
            for key in ("content", "artifact")
            if getattr(value, key, None) is not None
        }
    if module_name.startswith("mcp.") and hasattr(value, "model_dump"):
        dumped = value.model_dump()
        return dumped if isinstance(dumped, dict) else None
    return None


# ─────────────────────────────────────────────────────────
# TOOL RESULT NORMALIZATION
# Takes the unwrapped payload(s) and produces one clean, merged facts
# dict, then applies tool-specific post-processing.
# ─────────────────────────────────────────────────────────

def normalize_tool_result(tool_name: str, result: Any) -> dict[str, Any]:
    """Full normalization entry point: unwrap whatever shape `result`
    arrived in, filter each payload down to FACT_KEYS-allowed fields,
    merge multiple payloads together, then apply any tool-specific
    normalization (see _normalize_tool_specific_facts). Confirmed via
    direct testing to correctly handle both plain-dict and
    LangChain/MCP-wrapped tuple shapes.
    """
    payloads = unwrap_tool_result_payload(content=result)
    normalized: dict[str, Any] = {}
    for payload in payloads:
        payload_facts = _normalize_payload_mapping(payload)
        if payload_facts:
            normalized = _merge_fact_mappings(normalized, payload_facts)
    return _normalize_tool_specific_facts(tool_name, normalized)


def _normalize_payload_mapping(payload: dict | list) -> dict[str, Any]:
    """Normalize one unwrapped payload — a dict goes straight through
    _normalize_fact_mapping; a list of dicts becomes {"items": [...]}.
    """
    if isinstance(payload, dict):
        return _normalize_fact_mapping(payload)
    items = [
        _normalize_fact_mapping(item)
        for item in payload[:MAX_UNWRAP_LIST_ITEMS]
        if isinstance(item, dict)
    ]
    items = [item for item in items if item]
    return {"items": items} if items else {}


def _merge_fact_mappings(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    """Merge two normalized fact dicts. List-valued keys that represent
    collections of rows (items/stores/variants) get concatenated rather
    than overwritten, so multiple payloads' rows all survive. Scalar
    keys only get filled in if not already set (first payload wins).
    """
    merged = deepcopy(first)
    for key, value in second.items():
        if key in {"items", "stores", "variants"} and isinstance(value, list):
            existing = merged.get(key)
            if not isinstance(existing, list):
                merged[key] = deepcopy(value)
            else:
                existing.extend(deepcopy(value))
            continue
        if key not in merged or merged[key] in (None, "", []):
            merged[key] = deepcopy(value)
    return merged


def _normalize_tool_specific_facts(tool_name: str, facts: dict[str, Any]) -> dict[str, Any]:
    """Dispatch to a tool-specific normalizer for tools whose result
    shape needs extra massaging (stock/stores/fulfillment_options).
    Every other tool's facts pass through unchanged.
    """
    if tool_name == "stock":
        return _normalize_stock_facts(facts)
    if tool_name == "stores":
        return _normalize_store_facts(facts)
    if tool_name == "fulfillment_options":
        return _normalize_fulfillment_facts(facts)
    return facts


def _normalize_stock_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """Normalize stock() results: builds a per-variant "items" list
    (size/color/quantity/in_stock), backfilling product_id onto each
    item, and deriving in_stock from quantity/available when not
    already present. Falls back to synthesizing a single item from
    top-level fields if no variant list exists but enough scalar fields
    are present to describe one anyway.
    """
    product_id = facts.get("product_id")
    normalized = {key: facts[key] for key in ("product_id", "in_stock", "available", "quantity", "total_quantity", "size", "color") if key in facts}
    variants = facts.get("variants")
    items = []
    row_source = variants if isinstance(variants, list) else facts.get("items")
    if isinstance(row_source, list):
        for variant in row_source:
            if not isinstance(variant, dict):
                continue
            item = {key: variant[key] for key in ("product_id", "product_name", "name", "size", "color", "quantity", "total_quantity", "in_stock", "available") if key in variant}
            if product_id is not None and "product_id" not in item:
                item["product_id"] = product_id
            if "quantity" not in item and "total_quantity" in item:
                item["quantity"] = item["total_quantity"]
            if "quantity" in item:
                item["in_stock"] = bool(item["quantity"])
            elif "available" in item and "in_stock" not in item:
                item["in_stock"] = bool(item["available"])
            if item:
                items.append(item)
    if items:
        normalized["items"] = items
    elif any(key in normalized for key in ("quantity", "total_quantity", "in_stock", "available")) and any(key in normalized for key in ("size", "color")):
        item = {key: normalized[key] for key in ("product_id", "size", "color", "in_stock") if key in normalized}
        if "in_stock" not in item and "available" in normalized:
            item["in_stock"] = bool(normalized["available"])
        if "quantity" in normalized:
            item["quantity"] = normalized["quantity"]
        elif "total_quantity" in normalized:
            item["quantity"] = normalized["total_quantity"]
        if item:
            normalized["items"] = [item]
    return normalized


def _normalize_store_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """Normalize stores() results: builds a "stores" list with each
    row's store_id/store_name/address/quantity/in_stock, backfilling
    product_id onto each row. This is the function at the center of
    tonight's cross-store-fallback bug (see _filter_store_rows and
    _attach_validated_context) — confirmed correct in isolation; the
    bug was in when the filter got applied, not in this function.
    """
    product_id = facts.get("product_id")
    normalized = {
        key: facts[key]
        for key in (
            "product_id",
            "found",
            "requested_store_had_no_stock",
            "requested_store_name",
            "requested_size",
            "requested_color",
            "variant_available",
        )
        if key in facts
    }
    stores = facts.get("stores") if isinstance(facts.get("stores"), list) else facts.get("items")
    items = []
    if isinstance(stores, list):
        for store in stores:
            if not isinstance(store, dict):
                continue
            item = {key: store[key] for key in ("product_id", "product_name", "name", "store_id", "store_name", "address", "quantity", "in_stock", "available", "pickup_available", "distance_miles", "size", "color") if key in store}
            if product_id is not None:
                item["product_id"] = product_id
            if facts.get("requested_size") and "size" not in item:
                item["size"] = facts["requested_size"]
            if facts.get("requested_color") and "color" not in item:
                item["color"] = facts["requested_color"]
            if "quantity" in item:
                item["in_stock"] = bool(item["quantity"])
            elif "available" in item and "in_stock" not in item:
                item["in_stock"] = bool(item["available"])
            if item:
                items.append(item)
    if items:
        normalized["stores"] = items
    elif facts.get("variant_available") is False or (
        not items
        and (facts.get("requested_size") or facts.get("size") or facts.get("requested_color") or facts.get("color"))
    ):
        normalized["in_stock"] = False
        normalized["quantity"] = 0
        requested_size = facts.get("requested_size") or facts.get("size")
        requested_color = facts.get("requested_color") or facts.get("color")
        if requested_size:
            normalized["size"] = requested_size
        if requested_color:
            normalized["color"] = requested_color
    return normalized


def _normalize_fulfillment_facts(facts: dict[str, Any]) -> dict[str, Any]:
    """Normalize fulfillment_options() results: backfills product_id
    onto each pickup location and flattens delivery fields for claim
    proposal.
    """
    product_id = facts.get("product_id")
    normalized = {
        key: facts[key]
        for key in ("product_id", "found", "requested_size", "requested_color")
        if key in facts
    }
    pickup = facts.get("pickup")
    if isinstance(pickup, dict):
        locations = pickup.get("locations")
        if isinstance(locations, list):
            pickup["locations"] = [
                {**location, "product_id": product_id}
                if isinstance(location, dict) and product_id is not None
                else location
                for location in locations
            ]
        normalized["pickup"] = pickup
    delivery = facts.get("delivery")
    if isinstance(delivery, dict):
        if "available" in delivery:
            normalized["delivery_available"] = bool(delivery["available"])
        for source_key, target_key in (
            ("standard_estimate", "delivery_estimate"),
            ("quantity", "quantity"),
            ("size", "size"),
            ("color", "color"),
        ):
            if source_key in delivery:
                normalized[target_key] = delivery[source_key]
        normalized["delivery"] = delivery
    return normalized


# ─────────────────────────────────────────────────────────
# CONTEXT ENRICHMENT
# Cross-checks and fills in normalized facts using the tool call's own
# validated arguments and prior evidence in this sub-intent — e.g.
# attaching product_id onto every row, or linking a stock/stores call
# back to a single unambiguous prior search result.
# ─────────────────────────────────────────────────────────

def _attach_validated_context(tool_name: str, facts: dict[str, Any], args: dict[str, Any]) -> dict[str, Any]:
    """Backfill product_id/size/color (for stock) or
    product_id/store_id/requested_store_name (for stores/
    fulfillment_options) from the tool's own validated arguments onto
    the normalized facts and each row within them. For stock, also
    filters items down to the requested size if one was given, and
    synthesizes a single item from scalar fields if no item list
    exists. For stores, applies _filter_store_rows — EXCEPT when
    requested_store_had_no_stock is True, since in that case the
    returned stores are deliberately NOT the one requested (the
    cross-store fallback), and filtering them out would silently
    discard the fallback's whole result — this was a real, confirmed
    bug found and fixed during live debugging; see CLAUDE.md.
    """
    if tool_name == "stock":
        for key in ("product_id", "size", "color"):
            if args.get(key) and key not in facts:
                facts[key] = args[key]
        for item in facts.get("items", []) if isinstance(facts.get("items"), list) else []:
            if isinstance(item, dict):
                for key in ("product_id", "size", "color"):
                    if args.get(key) and key not in item:
                        item[key] = args[key]
        if args.get("size") and isinstance(facts.get("items"), list):
            requested_size = str(args["size"]).strip().upper()
            facts["items"] = [
                item
                for item in facts["items"]
                if not isinstance(item, dict)
                or not item.get("size")
                or str(item["size"]).strip().upper() == requested_size
            ]
        if not facts.get("items") and args.get("size") and any(key in facts for key in ("in_stock", "available", "quantity", "total_quantity")):
            item = {
                key: facts[key]
                for key in ("product_id", "in_stock", "size", "color", "quantity")
                if key in facts
            }
            if "in_stock" not in item and "available" in facts:
                item["in_stock"] = bool(facts["available"])
            if "quantity" not in item and "total_quantity" in facts:
                item["quantity"] = facts["total_quantity"]
            if item:
                facts["items"] = [item]
    if tool_name in {"stores", "fulfillment_options"}:
        if args.get("product_id") and "product_id" not in facts:
            facts["product_id"] = args["product_id"]
        if args.get("size") and "requested_size" not in facts:
            facts["requested_size"] = args["size"]
        if args.get("color") and "requested_color" not in facts:
            facts["requested_color"] = args["color"]
        if tool_name == "fulfillment_options":
            if args.get("size") and "size" not in facts:
                facts["size"] = args["size"]
            if args.get("color") and "color" not in facts:
                facts["color"] = args["color"]
        if args.get("store_id") and "store_id" not in facts:
            facts["store_id"] = args["store_id"]
        if args.get("store_name") and "requested_store_name" not in facts:
            facts["requested_store_name"] = args["store_name"]
        for item in facts.get("stores", []) if isinstance(facts.get("stores"), list) else []:
            if isinstance(item, dict):
                if args.get("product_id") and "product_id" not in item:
                    item["product_id"] = args["product_id"]
                if args.get("store_id") and "store_id" not in item:
                    item["store_id"] = args["store_id"]
                if args.get("store_name") and "store_name" not in item:
                    item["store_name"] = args["store_name"]
                if args.get("size") and "size" not in item:
                    item["size"] = args["size"]
                if args.get("color") and "color" not in item:
                    item["color"] = args["color"]
        if isinstance(facts.get("stores"), list) and not facts.get("requested_store_had_no_stock"):
            facts["stores"] = _filter_store_rows(facts["stores"], args)
        if (
            tool_name == "stores"
            and not facts.get("stores")
            and (args.get("size") or args.get("color"))
        ):
            facts["in_stock"] = False
            facts["quantity"] = 0
            if args.get("size"):
                facts["size"] = args["size"]
            if args.get("color"):
                facts["color"] = args["color"]
    return facts


def _filter_store_rows(rows: list, args: dict[str, Any]) -> list:
    """Narrow a stores list down to rows matching the requested
    store_id/store_name — used when the caller asked about a SPECIFIC
    store and got back rows for multiple stores. Only called when the
    requested store actually had stock (see _attach_validated_context);
    when it didn't and a cross-store fallback occurred, this filter is
    deliberately skipped, since it would otherwise discard every
    fallback row (none of which match the originally-requested store,
    by design).
    """
    requested_store_id = str(args.get("store_id", "")).strip().lower()
    requested_store_name = str(args.get("store_name", "")).strip().lower()
    if not requested_store_id and not requested_store_name:
        return rows
    filtered = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        row_store_id = str(row.get("store_id", "")).strip().lower()
        row_store_name = str(row.get("store_name", "")).strip().lower()
        if requested_store_id and row_store_id == requested_store_id:
            filtered.append(row)
        elif requested_store_name and row_store_name == requested_store_name:
            filtered.append(row)
    return filtered


def _link_prior_search_context(tool_name: str, facts: dict[str, Any], context: _EvidenceContext) -> dict[str, Any]:
    """If a stock/stores call has no product_id at all, and exactly one
    prior successful search() in this same sub-intent+attempt found
    exactly one product, backfill that product's ID/name onto these
    facts. Deliberately does nothing if the prior search was ambiguous
    (0 or 2+ candidates) — safer to leave product_id unset than guess.
    """
    if tool_name not in {"stock", "stores"} or _has_product_id(facts):
        return facts
    candidate = _unique_prior_search_product(context)
    if candidate is None:
        return facts
    product_id = candidate["product_id"]
    product_name = candidate.get("product_name") or candidate.get("name")
    facts.setdefault("product_id", product_id)
    if product_name:
        facts.setdefault("product_name", product_name)
        facts.setdefault("name", product_name)
    for key in ("items", "stores"):
        values = facts.get(key)
        if not isinstance(values, list):
            continue
        for item in values:
            if not isinstance(item, dict):
                continue
            item.setdefault("product_id", product_id)
            if product_name:
                item.setdefault("product_name", product_name)
                item.setdefault("name", product_name)
    return facts


def _has_product_id(facts: dict[str, Any]) -> bool:
    """True if facts (or any row within items/stores) already has a
    non-blank product_id.
    """
    if isinstance(facts.get("product_id"), str) and facts["product_id"].strip():
        return True
    return any(
        isinstance(item, dict)
        and isinstance(item.get("product_id"), str)
        and item["product_id"].strip()
        for key in ("items", "stores")
        for item in (facts.get(key) if isinstance(facts.get(key), list) else [])
    )


def _unique_prior_search_product(context: _EvidenceContext) -> dict[str, Any] | None:
    """Find the most recent successful search() in this exact
    sub-intent+attempt and return its product if there's EXACTLY one
    candidate. Returns None on ambiguity (0 or 2+ candidates) or if no
    matching search exists — deliberately conservative.
    """
    for entry in reversed(context.evidence_entries):
        if (
            not entry.success
            or entry.tool_name != "search"
            or entry.sub_intent_id != context.sub_intent_id
            or entry.attempt_number != context.attempt_number
        ):
            continue
        candidates = _search_product_candidates(entry.normalized_facts)
        if len(candidates) == 1:
            return candidates[0]
        return None
    return None


def _search_product_candidates(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Extract unique (product_id, product_name) candidates from a
    search() result's normalized facts.
    """
    candidates = []
    for item in _iter_fact_candidates(facts):
        product_id = item.get("product_id")
        if isinstance(product_id, str) and product_id.strip():
            candidates.append(
                {
                    "product_id": product_id,
                    "product_name": item.get("product_name") or item.get("name"),
                }
            )
    by_id = {}
    for candidate in candidates:
        by_id.setdefault(candidate["product_id"], candidate)
    return list(by_id.values())


def _iter_fact_candidates(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Yield the top-level facts dict plus every row inside its
    items/stores lists — a flat iteration surface for functions that
    need to check every "row" a result might contain.
    """
    candidates = [facts] if facts else []
    for key in ("items", "stores"):
        value = facts.get(key)
        if isinstance(value, list):
            candidates.extend(item for item in value if isinstance(item, dict))
    return candidates


# ─────────────────────────────────────────────────────────
# DIAGNOSTICS
# ─────────────────────────────────────────────────────────

def _record_fact_shape(tool_name: str, facts: dict[str, Any], args: dict[str, Any], sequence: int) -> None:
    """Log the shape of a tool result's normalized facts (which top-
    level/nested fields are present, how many "row" items, which
    entity IDs) — this is the "tool_evidence_shape" diagnostic event
    seen throughout tonight's debugging traces, and was the key signal
    that first revealed the cross-store-fallback bug (item_count: 0
    when it should have been 2).
    """
    diagnostics = get_diagnostics()
    if diagnostics is None or tool_name not in {"search", "stock", "stores", "fulfillment_options"}:
        return
    items = _iter_shape_items(facts)
    nested_keys = sorted({key for item in items for key in item if isinstance(key, str)})
    entity_ids = sorted({
        str(value)
        for item in [facts, *items]
        for key, value in item.items()
        if key in {"product_id", "store_id"} and isinstance(value, str)
    })
    diagnostics.record(
        "tool_evidence_shape",
        0,
        tool_name=tool_name,
        validated_arg_keys=",".join(sorted(args.keys())),
        top_level_fields=",".join(sorted(facts.keys())),
        nested_fields=",".join(nested_keys),
        entity_ids=",".join(entity_ids),
        sequence_number=sequence,
        item_count=len(items),
    )


def _iter_shape_items(facts: dict[str, Any]) -> list[dict[str, Any]]:
    """Flatten items/stores rows, plus any pickup.locations rows, into
    one list — used only for the diagnostic shape-logging above.
    """
    items = []
    for key in ("items", "stores"):
        value = facts.get(key)
        if isinstance(value, list):
            items.extend(item for item in value if isinstance(item, dict))
    pickup = facts.get("pickup")
    if isinstance(pickup, dict) and isinstance(pickup.get("locations"), list):
        items.extend(item for item in pickup["locations"] if isinstance(item, dict))
    return items


def _entity_from_facts(facts: dict[str, Any]) -> tuple[str | None, str | None]:
    """Identify the primary entity (product/external_product/order/
    store/customer) this evidence is about, for tagging on
    EvidenceEntry.entity_type/entity_id.
    """
    candidates = facts.get("items") if isinstance(facts.get("items"), list) else [facts]
    for item in candidates:
        if not isinstance(item, dict):
            continue
        for entity_type, key in (
            ("product", "product_id"),
            ("external_product", "external_product_id"),
            ("order", "order_id"),
            ("store", "store_id"),
            ("customer", "customer_id"),
        ):
            entity_id = item.get(key)
            if isinstance(entity_id, str) and entity_id.strip():
                return entity_type, entity_id
    return None, None


def _classify_guard_denial(error_message: str) -> str:
    """Map a ToolCallGuard denial's message text to a specific error
    code, so callers get a consistent code rather than parsing text.
    """
    if "Tool call limit reached" in error_message:
        return ERROR_TOOL_CALL_LIMIT_EXCEEDED
    if "already made this turn" in error_message:
        return ERROR_REPEATED_TOOL_CALL
    return ERROR_TOOL_EXECUTION_FAILED


# ─────────────────────────────────────────────────────────
# PUBLIC ENTRY POINT — called by tool_guard.py for every tool call
# ─────────────────────────────────────────────────────────

def record_tool_call(
    *,
    tool_name: str,
    validated_args: Any,
    success: bool,
    result: Any = None,
    error_code: str | None = None,
    agent_name: str | None = None,
    original_args: Any | None = None,
) -> ToolCallRecord | None:
    """Record one tool call attempt. If it succeeded, attempts to
    normalize the result into evidence; if normalization itself raises
    (e.g. an unnormalizable/circular result), the RECORDED success is
    downgraded to False and error_code is set to
    ERROR_EVIDENCE_NORMALIZATION_FAILED — even though the caller passed
    success=True — because no usable evidence was actually produced.

    This downgrade (via `recorded_success`) is a deliberate fix: without
    it, a record could end up with success=True, error_code=set, and
    evidence_id=None simultaneously, a genuinely self-contradictory
    state confirmed via direct testing with a circular-reference input
    that breaks deepcopy() inside normalize_tool_result. Any code
    checking only `.success` to decide whether a tool call produced
    real evidence would be misled without this fix.

    Returns None if no evidence context is currently active (nothing
    to record into).
    """
    context = _active_evidence_context.get()
    if context is None:
        return None

    sequence = context.next_sequence
    context.next_sequence += 1

    resolved_agent_name = agent_name or context.current_agent or "unknown_agent"
    safe_args = normalize_args(validated_args)
    safe_original_args = normalize_args(validated_args if original_args is None else original_args)
    evidence_id = None
    recorded_success = success

    if success:
        try:
            facts = normalize_tool_result(tool_name, result)
            facts = _attach_validated_context(tool_name, facts, safe_args)
            facts = _link_prior_search_context(tool_name, facts, context)
            _record_fact_shape(tool_name, facts, safe_args, sequence)
            if facts:
                entity_type, entity_id = _entity_from_facts(facts)
                evidence = EvidenceEntry(
                    sequence=sequence,
                    sub_intent_id=context.sub_intent_id,
                    attempt_number=context.attempt_number,
                    agent_name=resolved_agent_name,
                    tool_name=tool_name,
                    original_args=safe_original_args,
                    validated_args=safe_args,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    normalized_facts=facts,
                    success=True,
                )
                context.evidence_entries.append(evidence)
                evidence_id = evidence.evidence_id
        except Exception:
            error_code = ERROR_EVIDENCE_NORMALIZATION_FAILED
            recorded_success = False

    record = ToolCallRecord(
        sequence=sequence,
        sub_intent_id=context.sub_intent_id,
        attempt_number=context.attempt_number,
        agent_name=resolved_agent_name,
        tool_name=tool_name,
        original_args=safe_original_args,
        validated_args=safe_args,
        success=recorded_success,
        evidence_id=evidence_id,
        error_code=error_code,
    )
    context.tool_call_records.append(record)
    return record


def classify_guard_denial(error_message: str) -> str:
    """Public wrapper around _classify_guard_denial."""
    return _classify_guard_denial(error_message)
