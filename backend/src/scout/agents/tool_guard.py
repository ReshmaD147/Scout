import contextvars
import time
from typing import Any

from langchain_core.tools import StructuredTool

from scout.agents.diagnostics import record_tool_call_timing
from scout.agents.evidence import (
    ERROR_ARGUMENT_VALIDATION_FAILED,
    ERROR_TOOL_EXECUTION_FAILED,
    classify_guard_denial,
    record_tool_call,
    set_current_agent,
)

_active_guard: contextvars.ContextVar = contextvars.ContextVar("active_guard", default=None)
_inventory_argument_constraints: contextvars.ContextVar = contextvars.ContextVar(
    "inventory_argument_constraints",
    default=None,
)

ARGUMENT_NORMALIZATION_ALLOWLIST = {
    "stock": {"product_id"},
    "stores": {"product_id"},
    "fulfillment_options": {"product_id"},
}

SAFE_TOOL_ARGUMENT_FAILURE = (
    "Tool argument validation failed. Continue without exposing internal errors."
)


class ToolArgumentNormalizationError(ValueError):
    pass


SIZE_ALIASES = {
    "extra small": "XS",
    "x-small": "XS",
    "xsmall": "XS",
    "small": "S",
    "medium": "M",
    "large": "L",
    "extra large": "XL",
    "x-large": "XL",
    "xlarge": "XL",
}


def set_inventory_argument_constraints(constraints: dict | None) -> None:
    safe_constraints = {}
    if isinstance(constraints, dict):
        for key in ("product_id", "size", "color"):
            value = constraints.get(key)
            if isinstance(value, str) and value.strip():
                safe_constraints[key] = value.strip()
    _inventory_argument_constraints.set(safe_constraints or None)


def clear_inventory_argument_constraints() -> None:
    _inventory_argument_constraints.set(None)


def _normalize_inventory_size(value: str) -> str:
    stripped = value.strip()
    return SIZE_ALIASES.get(stripped.lower(), stripped).upper()


def _normalize_inventory_color(value: str) -> str:
    return value.strip().lower()


def normalize_tool_arguments(*, tool_name: str, arguments: dict) -> dict:
    normalized = dict(arguments)
    fields = ARGUMENT_NORMALIZATION_ALLOWLIST.get(tool_name, set())

    for field in fields:
        if field not in normalized:
            continue
        value = normalized[field]
        if isinstance(value, str):
            if not value.strip():
                raise ToolArgumentNormalizationError(f"{tool_name}.{field} cannot be blank")
            continue
        if isinstance(value, list):
            if (
                len(value) == 1
                and isinstance(value[0], str)
                and value[0].strip()
            ):
                normalized[field] = value[0]
                continue
            raise ToolArgumentNormalizationError(f"{tool_name}.{field} must be one string")
        raise ToolArgumentNormalizationError(f"{tool_name}.{field} must be a string")

    return _enrich_inventory_tool_arguments(tool_name=tool_name, arguments=normalized)


def _enrich_inventory_tool_arguments(*, tool_name: str, arguments: dict) -> dict:
    if tool_name not in {"stock", "stores", "fulfillment_options"}:
        return arguments

    constraints = _inventory_argument_constraints.get()
    if not isinstance(constraints, dict):
        return arguments

    enriched = dict(arguments)
    product_constraint = constraints.get("product_id")
    if isinstance(product_constraint, str) and product_constraint.strip():
        existing_product = enriched.get("product_id")
        if isinstance(existing_product, str) and existing_product.strip():
            if existing_product.strip() != product_constraint.strip():
                raise ToolArgumentNormalizationError(
                    f"{tool_name}.product_id conflicts with structured request context"
                )
        elif existing_product in (None, "") or "product_id" not in enriched:
            enriched["product_id"] = product_constraint.strip()
        else:
            raise ToolArgumentNormalizationError(f"{tool_name}.product_id must be a string")

    for field, normalizer in (
        ("size", _normalize_inventory_size),
        ("color", _normalize_inventory_color),
    ):
        constraint_value = constraints.get(field)
        if not isinstance(constraint_value, str) or not constraint_value.strip():
            continue
        existing_value = enriched.get(field)
        normalized_constraint = normalizer(constraint_value)
        if isinstance(existing_value, str) and existing_value.strip():
            if normalizer(existing_value) != normalized_constraint:
                raise ToolArgumentNormalizationError(
                    f"{tool_name}.{field} conflicts with structured request context"
                )
            if field in {"size", "color"}:
                enriched[field] = normalized_constraint
            continue
        if existing_value in (None, ""):
            enriched[field] = normalized_constraint
            continue
        if field not in enriched:
            enriched[field] = normalized_constraint
            continue
        raise ToolArgumentNormalizationError(f"{tool_name}.{field} must be a string")

    return enriched


class ToolCallGuard:
    def __init__(self, max_total_calls: int = 10, max_identical_calls: int = 1):
        self.max_total_calls = max_total_calls
        self.max_identical_calls = max_identical_calls
        self.total_calls = 0
        self.call_signatures: dict[tuple, int] = {}

    def _signature(self, tool_name: str, args: dict) -> tuple:
        try:
            normalized = tuple(sorted((k, str(v)) for k, v in args.items()))
        except Exception:
            normalized = (str(args),)
        return (tool_name, normalized)

    def check(self, tool_name: str, args: dict) -> tuple[bool, str]:
        if self.total_calls >= self.max_total_calls:
            return False, (
                f"Tool call limit reached ({self.max_total_calls} calls this turn). "
                "Stop making tool calls and respond to the customer with what you "
                "already know, or tell them you're having trouble completing this request."
            )

        sig = self._signature(tool_name, args)
        if self.call_signatures.get(sig, 0) >= self.max_identical_calls:
            return False, (
                f"This exact tool call ({tool_name} with these same arguments) was "
                "already made this turn. Do not repeat it — use the result you already "
                "have, try different arguments, or respond to the customer directly."
            )

        return True, ""

    def record(self, tool_name: str, args: dict) -> None:
        self.total_calls += 1
        sig = self._signature(tool_name, args)
        self.call_signatures[sig] = self.call_signatures.get(sig, 0) + 1


def reset_guard(max_total_calls: int = 10, max_identical_calls: int = 1) -> ToolCallGuard:
    guard = ToolCallGuard(max_total_calls, max_identical_calls)
    _active_guard.set(guard)
    return guard


def _safe_record_tool_call(**kwargs) -> None:
    try:
        record_tool_call(**kwargs)
    except Exception:
        pass


def _error_code_for_exception(exc: Exception) -> str:
    if exc.__class__.__name__ in {"ValidationError", "SchemaValidationError"}:
        return ERROR_ARGUMENT_VALIDATION_FAILED
    message = str(exc).lower()
    if "validation error" in message or "input should be" in message:
        return ERROR_ARGUMENT_VALIDATION_FAILED
    return ERROR_TOOL_EXECUTION_FAILED


def _call_args_from_invocation(args: tuple, kwargs: dict) -> dict:
    if kwargs:
        return dict(kwargs)
    if args and isinstance(args[0], dict):
        return dict(args[0])
    return {}


def _normalized_invocation(args: tuple, kwargs: dict, normalized_args: dict) -> tuple[tuple, dict]:
    if kwargs:
        return args, normalized_args
    if args and isinstance(args[0], dict):
        return (normalized_args, *args[1:]), kwargs
    return args, kwargs


def wrap_tool_with_guard(tool: StructuredTool, agent_name: str | None = None) -> StructuredTool:
    original_coroutine = tool.coroutine

    async def guarded_coroutine(*args, **kwargs) -> Any:
        started = time.perf_counter()
        if agent_name:
            set_current_agent(agent_name)

        guard = _active_guard.get()
        original_call_args = _call_args_from_invocation(args, kwargs)

        try:
            call_args = normalize_tool_arguments(
                tool_name=tool.name,
                arguments=original_call_args,
            )
        except ToolArgumentNormalizationError:
            _safe_record_tool_call(
                tool_name=tool.name,
                original_args=original_call_args,
                validated_args={},
                success=False,
                error_code=ERROR_ARGUMENT_VALIDATION_FAILED,
                agent_name=agent_name,
            )
            record_tool_call_timing(
                tool_name=tool.name,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                agent_name=agent_name,
            )
            return SAFE_TOOL_ARGUMENT_FAILURE

        if guard is not None:
            allowed, reason = guard.check(tool.name, call_args)
            if not allowed:
                _safe_record_tool_call(
                    tool_name=tool.name,
                    original_args=original_call_args,
                    validated_args=call_args,
                    success=False,
                    error_code=classify_guard_denial(reason),
                    agent_name=agent_name,
                )
                record_tool_call_timing(
                    tool_name=tool.name,
                    elapsed_ms=(time.perf_counter() - started) * 1000,
                    agent_name=agent_name,
                )
                return reason
            guard.record(tool.name, call_args)

        try:
            if original_coroutine is not None:
                normalized_args, normalized_kwargs = _normalized_invocation(
                    args, kwargs, call_args
                )
                result = await original_coroutine(*normalized_args, **normalized_kwargs)
            else:
                normalized_args, normalized_kwargs = _normalized_invocation(
                    args, kwargs, call_args
                )
                result = tool.func(*normalized_args, **normalized_kwargs)
        except Exception as exc:
            error_code = _error_code_for_exception(exc)
            _safe_record_tool_call(
                tool_name=tool.name,
                original_args=original_call_args,
                validated_args=call_args,
                success=False,
                error_code=error_code,
                agent_name=agent_name,
            )
            record_tool_call_timing(
                tool_name=tool.name,
                elapsed_ms=(time.perf_counter() - started) * 1000,
                agent_name=agent_name,
            )
            if error_code == ERROR_ARGUMENT_VALIDATION_FAILED:
                return SAFE_TOOL_ARGUMENT_FAILURE
            raise

        _safe_record_tool_call(
            tool_name=tool.name,
            original_args=original_call_args,
            validated_args=call_args,
            success=True,
            result=result,
            agent_name=agent_name,
        )
        record_tool_call_timing(
            tool_name=tool.name,
            elapsed_ms=(time.perf_counter() - started) * 1000,
            agent_name=agent_name,
        )
        return result

    return StructuredTool(
        name=tool.name,
        description=tool.description,
        args_schema=tool.args_schema,
        coroutine=guarded_coroutine,
        func=None,
    )


def wrap_tools_with_guard(
    tools: list[StructuredTool], agent_name: str | None = None
) -> list[StructuredTool]:
    return [wrap_tool_with_guard(t, agent_name=agent_name) for t in tools]
