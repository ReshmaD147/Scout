import json
import re
import sys
import time
from pathlib import Path

import requests

API_BASE = "http://127.0.0.1:8000"
TEST_CASES_PATH = Path(__file__).parent / "test_cases.json"
CONVERSATION_TEST_CASES_PATH = Path(__file__).parent / "conversation_test_cases.json"
ROUTING_TEST_CASES_PATH = Path(__file__).parent / "routing_test_cases.json"


def run_test_case(tc: dict, max_retries: int = 1) -> dict:
    last_error = None
    for attempt in range(max_retries + 1):
        try:
            response = requests.post(
                f"{API_BASE}/chat",
                json={"message": tc["query"]},
                timeout=120,
            )
            response.raise_for_status()
            data = response.json()
            last_error = None
            break
        except Exception as e:
            last_error = e
            if attempt < max_retries:
                time.sleep(3)  # brief pause before retrying a transient failure

    if last_error is not None:
        return {
            "id": tc["id"],
            "passed": False,
            "reason": f"Request failed after {max_retries + 1} attempt(s): {last_error}",
            "reply": None,
        }

    reply = data.get("reply", "")
    reply_lower = reply.lower()
    products = data.get("products", [])

    failures = []

    if tc.get("expect_products") and not products:
        failures.append("expected products in response, got none")

    must_contain_any = tc.get("must_contain_any", [])
    if must_contain_any and not any(phrase.lower() in reply_lower for phrase in must_contain_any) and not _semantic_expectation_passes(tc, reply, products):
        failures.append(f"reply missing any of: {must_contain_any}")

    must_not_contain = tc.get("must_not_contain", [])
    found_forbidden = [p for p in must_not_contain if p.lower() in reply_lower]
    if found_forbidden:
        failures.append(f"reply contains forbidden phrase(s): {found_forbidden}")

    return {
        "id": tc["id"],
        "description": tc["description"],
        "passed": len(failures) == 0,
        "failures": failures,
        "reply": reply,
        "product_count": len(products),
    }


def run_conversation_test_case(tc: dict, max_retries: int = 1) -> dict:
    """Runs a sequence of turns against the SAME session_id, checking each
    turn's response for correct handling — including memory of earlier
    turns. A later turn failing to resolve context from an earlier one
    (e.g. asking "which product?" after it was already named) is exactly
    the kind of regression a single-turn eval can't catch."""
    session_id = None
    turn_results = []

    for i, turn in enumerate(tc["turns"]):
        last_error = None
        data = None
        for attempt in range(max_retries + 1):
            try:
                payload = {"message": turn["query"]}
                if session_id:
                    payload["session_id"] = session_id
                response = requests.post(f"{API_BASE}/chat", json=payload, timeout=120)
                response.raise_for_status()
                data = response.json()
                last_error = None
                break
            except Exception as e:
                last_error = e
                if attempt < max_retries:
                    time.sleep(3)

        if last_error is not None:
            error_str = str(last_error)
            is_rate_limit = "429" in error_str or "rate_limit" in error_str.lower()
            turn_results.append({
                "turn": i + 1,
                "query": turn["query"],
                "passed": False,
                "skipped_due_to_quota": is_rate_limit,
                "reason": f"Request failed: {last_error}",
            })
            continue

        session_id = data.get("session_id", session_id)
        reply_lower = data.get("reply", "").lower()
        products = data.get("products", [])

        failures = []
        if turn.get("expect_products") and not products:
            failures.append("expected products, got none")

        must_contain_any = turn.get("must_contain_any", [])
        if must_contain_any and not any(p.lower() in reply_lower for p in must_contain_any) and not _semantic_expectation_passes(turn, data.get("reply", ""), products):
            failures.append(f"reply missing any of: {must_contain_any}")

        must_not_contain = turn.get("must_not_contain", [])
        found_forbidden = [p for p in must_not_contain if p.lower() in reply_lower]
        if found_forbidden:
            failures.append(f"reply contains forbidden phrase(s): {found_forbidden}")

        turn_results.append({
            "turn": i + 1,
            "query": turn["query"],
            "passed": len(failures) == 0,
            "failures": failures,
            "reply": data.get("reply", ""),
        })

    any_quota_issue = any(t.get("skipped_due_to_quota") for t in turn_results)
    all_passed = all(t["passed"] for t in turn_results)

    return {
        "id": tc["id"],
        "description": tc["description"],
        "passed": all_passed,
        "skipped_due_to_quota": any_quota_issue and not all_passed,
        "turns": turn_results,
    }


def _semantic_expectation_passes(tc: dict, reply: str, products: list[dict]) -> bool:
    query = (tc.get("query") or "").lower()
    reply_lower = (reply or "").lower()
    if _asks_inventory_unavailability(query, tc):
        return _mentions_requested_size(query, reply_lower) and _mentions_unavailable(reply_lower) and not _asserts_available(reply_lower)
    return False


def _asks_inventory_unavailability(query: str, tc: dict) -> bool:
    expected = " ".join(tc.get("must_contain_any", [])).lower()
    return (
        any(term in query for term in ("available", "stock", "size", "medium", "large", "small"))
        and any(term in expected for term in ("out of stock", "medium", "size"))
    )


def _mentions_requested_size(query: str, reply: str) -> bool:
    size_groups = [
        (("medium", " size m", "size m", "(m)", " m "), ("medium", r"\bsize\s+m\b", r"\(m\)")),
        (("large", " size l", "size l", "(l)", " l "), ("large", r"\bsize\s+l\b", r"\(l\)")),
        (("small", " size s", "size s", "(s)", " s "), ("small", r"\bsize\s+s\b", r"\(s\)")),
    ]
    for query_terms, reply_terms in size_groups:
        if any(term in query for term in query_terms):
            return any(re.search(term, reply) for term in reply_terms)
    return True


def _mentions_unavailable(reply: str) -> bool:
    return any(
        phrase in reply
        for phrase in (
            "out of stock",
            "unavailable",
            "not available",
            "not currently available",
            "no stock",
            "0 units",
            "zero units",
        )
    )


def _asserts_available(reply: str) -> bool:
    return " is available " in reply and not _mentions_unavailable(reply)


def main():
    test_cases = json.loads(TEST_CASES_PATH.read_text())
    conversation_test_cases = json.loads(CONVERSATION_TEST_CASES_PATH.read_text())
    routing_test_cases = json.loads(ROUTING_TEST_CASES_PATH.read_text())

    try:
        requests.get(f"{API_BASE}/health", timeout=5).raise_for_status()
    except Exception:
        print(f"ERROR: backend not reachable at {API_BASE}. Is uvicorn running?")
        sys.exit(1)

    results = []
    print("--- Single-turn test cases ---")
    for tc in test_cases:
        print(f"Running {tc['id']}: {tc['description']}...")
        result = run_test_case(tc)
        results.append(result)
        status = "PASS" if result["passed"] else "FAIL"
        print(f"  [{status}]")
        if not result["passed"]:
            for f in result.get("failures", [result.get("reason", "unknown error")]):
                print(f"    - {f}")
        time.sleep(1)

    print("\n--- Multi-turn conversation test cases ---")
    for tc in conversation_test_cases:
        print(f"Running {tc['id']}: {tc['description']}...")
        result = run_conversation_test_case(tc)
        results.append(result)
        status = "PASS" if result["passed"] else "FAIL"
        print(f"  [{status}]")
        if not result["passed"]:
            for t in result["turns"]:
                if not t["passed"]:
                    print(f"    Turn {t['turn']} (\"{t['query']}\"): {t.get('failures', [t.get('reason')])}")
        time.sleep(1)

    print("\n--- Phase 2: 5-agent routing test cases ---")
    for tc in routing_test_cases:
        print(f"Running {tc['id']}: {tc['description']}...")
        result = run_test_case(tc)
        results.append(result)
        status = "PASS" if result["passed"] else "FAIL"
        print(f"  [{status}]")
        if not result["passed"]:
            for f in result.get("failures", [result.get("reason", "unknown error")]):
                print(f"    - {f}")
        time.sleep(1)

    passed = sum(1 for r in results if r["passed"])
    total = len(results)

    print(f"\n{'=' * 50}")
    print(f"RESULTS: {passed}/{total} passed")
    print(f"{'=' * 50}")

    for r in results:
        status = "✓" if r["passed"] else "✗"
        print(f"{status} {r['id']}: {r.get('description', '')}")

    output_path = Path(__file__).parent / "last_run_results.json"
    output_path.write_text(json.dumps(results, indent=2))
    print(f"\nFull results saved to {output_path}")

    sys.exit(0 if passed == total else 1)


if __name__ == "__main__":
    main()
