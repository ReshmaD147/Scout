"""End-to-end customer journey tests for Scout/Lumi.

Simulates real, complete customer journeys through the actual, live API -
exactly as a real browser session would call it. Not a demo script - this
is meant to be run before a real demo/deploy to catch genuine regressions
across the full user experience, from first message to completed purchase.

Run with the backend live:
    uvicorn scout.main:app --reload --app-dir src

Then:
    python3 tests/e2e/test_customer_journeys.py
"""

import sys
import time
import requests

API_BASE = "http://127.0.0.1:8000"


class Journey:
    """A single, stateful customer session - carries session_id across
    every request, exactly like a real browser would."""

    def __init__(self, name: str):
        self.name = name
        self.session_id = None
        self.steps = []
        self.cart_items = []

    def chat(self, message: str, expect_products: bool = None, must_contain_any: list = None,
              must_not_contain: list = None) -> dict:
        payload = {"message": message}
        if self.session_id:
            payload["session_id"] = self.session_id
        response = requests.post(f"{API_BASE}/chat", json=payload, timeout=90)
        data = response.json()
        self.session_id = data.get("session_id", self.session_id)

        reply = data.get("reply", "")
        reply_lower = reply.lower()
        products = data.get("products", [])
        failures = []

        if expect_products and not products:
            failures.append("expected products, got none")
        if must_contain_any and not any(p.lower() in reply_lower for p in must_contain_any):
            failures.append(f"reply missing any of: {must_contain_any}")
        if must_not_contain:
            found = [p for p in must_not_contain if p.lower() in reply_lower]
            if found:
                failures.append(f"reply contains forbidden phrase(s): {found}")

        self.steps.append({
            "action": f'chat("{message}")',
            "reply": reply,
            "products": [p.get("name") for p in products],
            "passed": not failures,
            "failures": failures,
        })
        return data

    def add_to_cart_from_chat(self, product_id: str, recommendation_id: str = None) -> dict:
        """Simulates clicking 'Add to Cart' on a chat product card."""
        payload = {"product_id": product_id, "quantity": 1}
        if recommendation_id:
            payload["recommendation_id"] = recommendation_id
            payload["recommendation_session_id"] = self.session_id
        response = requests.post(f"{API_BASE}/cart/add", json=payload, timeout=30)
        data = response.json()
        passed = data.get("success", False)
        self.steps.append({
            "action": f"add_to_cart_from_chat({product_id})",
            "result": data,
            "passed": passed,
            "failures": [] if passed else [data.get("error", "unknown failure")],
        })
        if passed:
            self.cart_items.append({
                "product_id": data.get("product_id"),
                "quantity": 1,
                "size": data.get("size"),
                "color": data.get("color"),
                "attribution_source": data.get("attribution_source"),
                "recommendation_id": data.get("recommendation_id"),
                "recommendation_session_id": self.session_id if data.get("attribution_source") else None,
            })
        return data

    def sign_in(self, customer_id: str = "C001") -> dict:
        response = requests.post(f"{API_BASE}/demo-auth/sign-in", json={"customer_id": customer_id}, timeout=30)
        data = response.json()
        self.session_id = data.get("session_id", self.session_id)
        self.steps.append({
            "action": f"sign_in({customer_id})",
            "passed": bool(data.get("session_id")),
            "failures": [] if data.get("session_id") else ["no session_id returned"],
        })
        return data

    def checkout(self, expect_success: bool = True) -> dict:
        """Simulates completing checkout with test contact/shipping/card details."""
        payload = {
            "items": self.cart_items,
            "session_id": self.session_id,
            "contact_email": "test.customer@example.com",
            "shipping_address": {
                "full_name": "Test Customer",
                "address_line1": "123 Test St",
                "city": "Minneapolis",
                "state": "MN",
                "postal_code": "55401",
                "country": "US",
            },
            "billing_same_as_shipping": True,
        }
        response = requests.post(f"{API_BASE}/checkout", json=payload, timeout=30)
        data = response.json()
        passed = data.get("success", False) == expect_success
        self.steps.append({
            "action": "checkout()",
            "result": data,
            "passed": passed,
            "failures": [] if passed else [f"expected success={expect_success}, got {data}"],
        })
        return data

    def report(self):
        all_passed = all(s["passed"] for s in self.steps)
        status = "PASS" if all_passed else "FAIL"
        print(f"\n{'=' * 70}")
        print(f"[{status}] Journey: {self.name}")
        print(f"{'=' * 70}")
        for i, step in enumerate(self.steps, 1):
            mark = "\u2713" if step["passed"] else "\u2717"
            print(f"  {mark} Step {i}: {step['action']}")
            if "reply" in step:
                print(f"      Reply: {step['reply'][:120]}")
            if not step["passed"]:
                for f in step["failures"]:
                    print(f"      FAILURE: {f}")
        return all_passed


# ---------------------------------------------------------------------------
# Journey 1: New, undecided customer - vague request, narrows down, browses,
# leaves without buying (a genuinely common real pattern to make sure
# doesn't break/crash anything).
# ---------------------------------------------------------------------------

def journey_browsing_only():
    j = Journey("Browsing customer - vague request, narrows down, no purchase")
    j.chat("I need something to wear")
    j.chat("A dress, under $80", expect_products=True)
    j.chat("Do you have anything in black?")
    j.chat("What about for a night out?")
    return j.report()


# ---------------------------------------------------------------------------
# Journey 2: Decisive customer - clear request straight to purchase, the
# core, highest-value path.
# ---------------------------------------------------------------------------

def journey_recommend_to_purchase():
    j = Journey("Decisive customer - recommendation straight through to purchase")
    result = j.chat("Recommend a dress under $80", expect_products=True)
    products = result.get("products", [])
    if not products:
        j.report()
        return False
    first_product = products[0]
    j.add_to_cart_from_chat(
        first_product["product_id"],
        recommendation_id=first_product.get("recommendation_id"),
    )
    j.checkout(expect_success=True)
    return j.report()


# ---------------------------------------------------------------------------
# Journey 3: Size-conscious customer - checks availability across sizes
# before deciding, exactly the interrupted-clarification pattern tested
# extensively.
# ---------------------------------------------------------------------------

def journey_size_check_then_purchase():
    j = Journey("Size-conscious customer - checks multiple sizes before buying")
    j.chat("Is the Black Midi Dress available in medium?")
    j.chat("Is it available in large?")
    j.chat("Can you add it to my cart?", must_contain_any=["added", "done"])
    return j.report()


# ---------------------------------------------------------------------------
# Journey 4: Bargain hunter - Scout doesn't carry it, honest external
# fallback.
# ---------------------------------------------------------------------------

def journey_out_of_catalog_fallback():
    j = Journey("Bargain hunter - item not in catalog, honest external fallback")
    j.chat("Do you have a red cocktail dress under $100?")
    return j.report()


# ---------------------------------------------------------------------------
# Journey 5: Returning customer checking an order - authenticated flow.
# ---------------------------------------------------------------------------

def journey_returning_customer_order_check():
    j = Journey("Returning, signed-in customer checks a past order")
    j.sign_in("C001")
    j.chat(
        "Where is order O1001?",
        must_contain_any=["order", "shipped", "transit", "delivered", "pending", "processing"],
    )
    return j.report()


# ---------------------------------------------------------------------------
# Journey 6: Policy-curious customer - asks about returns before buying,
# a genuinely common pre-purchase question.
# ---------------------------------------------------------------------------

def journey_policy_question_before_buying():
    j = Journey("Policy-curious customer - asks about returns before deciding")
    j.chat("What is your return policy?")
    j.chat("How long do refunds take?")
    j.chat("Recommend a dress under $80", expect_products=True)
    return j.report()


# ---------------------------------------------------------------------------
# Journey 7: Distracted customer - topic switches mid-conversation, tests
# that context doesn't leak between unrelated topics.
# ---------------------------------------------------------------------------

def journey_distracted_topic_switching():
    j = Journey("Distracted customer - switches topics mid-conversation")
    j.chat("Recommend a dress under $80", expect_products=True)
    j.chat("Actually, do you have any shoes?", expect_products=True)
    j.chat("What's your return policy?")
    j.chat("yes", must_not_contain=["added the"])  # stale "yes" must not silently confirm anything
    return j.report()


# ---------------------------------------------------------------------------
# Journey 8: Out-of-scope tester - a real user poking at the edges.
# ---------------------------------------------------------------------------

def journey_out_of_scope_probing():
    j = Journey("Curious customer - tests what Scout can't do")
    j.chat("What's the weather today?", must_not_contain=["temperature", "degrees"])
    j.chat("Can you charge my card and buy this for me?", must_not_contain=["charged", "payment processed"])
    j.chat("Recommend a dress under $80", expect_products=True)  # confirm normal use still works after
    return j.report()


if __name__ == "__main__":
    try:
        requests.get(f"{API_BASE}/health", timeout=5).raise_for_status()
    except Exception:
        print(f"ERROR: backend not reachable at {API_BASE}. Is uvicorn running?")
        sys.exit(1)

    journeys = [
        journey_browsing_only,
        journey_recommend_to_purchase,
        journey_size_check_then_purchase,
        journey_out_of_catalog_fallback,
        journey_returning_customer_order_check,
        journey_policy_question_before_buying,
        journey_distracted_topic_switching,
        journey_out_of_scope_probing,
    ]

    results = []
    for journey_fn in journeys:
        try:
            results.append(journey_fn())
        except Exception as e:
            print(f"\n[ERROR] {journey_fn.__name__} crashed: {e}")
            results.append(False)
        time.sleep(1)

    passed = sum(results)
    total = len(results)
    print(f"\n{'=' * 70}")
    print(f"OVERALL: {passed}/{total} customer journeys passed")
    print(f"{'=' * 70}")
    sys.exit(0 if passed == total else 1)
