# Demo Script

## Opening

“Scout is a full-stack retail assistant where the model can help customers shop, but it cannot move money or mutate commerce state. Recommendations, inventory, policy, and order facts are evidence-backed before they reach the UI.”

## Setup

```bash
cd backend
source venv/bin/activate
ENABLE_STRIPE_MCP=false MODEL_PROVIDER=ollama \
python -m uvicorn scout.main:app --host 127.0.0.1 --port 8000
```

```bash
cd frontend
npm run dev
```

Open the storefront, then open the floating Scout chat widget.

## Beat 1 — Recommendation With Product Cards

Ask: “Recommend a dress under $80.”

Expected demo point:

- Scout returns verified Scout products under budget.
- Product cards come from approved product claims, not raw model prose.
- Mention that price grounding is checked again after rendering.

## Beat 2 — Inventory Boundary

Ask: “Is the black midi dress in a medium?”

Expected demo point:

- Scout answers medium black stock specifically.
- Product identity, color, size, and inventory are linked through evidence.
- Product summary alone is not treated as an inventory answer.

## Beat 2b — Contextual Selection, Interruption, and Cart Confirmation

Ask: "I like the second one." (following Beat 1's product list)

Then: "Wait, is it available in medium first?"

Then: "Yes."

Expected demo point:

- Scout resolves "the second one" against the products just shown, not a
  stale reference from earlier in the conversation.
- The clarifying question ("is it available in medium?") correctly checks
  the SAME product the customer just selected, even though it interrupts
  the pending cart decision.
- The cart offer survives the interruption - "yes" still confirms the
  original selection, not something stale or unrelated.
- The AI never adds anything to the cart without this explicit
  confirmation step.

## Beat 3 — Store Availability Without Pickup Overclaim

Ask: “Is it available at Maple Grove?”

Expected demo point:

- Context resolves “it” to the product already discussed.
- Store availability is verified against store evidence.
- Scout does not infer a pickup promise beyond available stock.

## Beat 4 — Dependent Multi-Intent Supervisor Path

Ask: “Recommend a black dress under $80, check whether it is available in medium at Maple Grove, and explain whether I can return it after opening it.”

Expected demo point:

- Supervisor path handles dependent subgoals.
- Recommendation runs before inventory when no product ID is initially known.
- Policy facts remain separate from product and inventory facts.

## Beat 5 — External Offer Fallback

Ask: “Find waterproof hiking shoes under $70. If Scout has none, show an outside option and explain its return-policy limitation.”

Expected demo point:

- Scout establishes internal insufficiency before external fallback.
- Third-party offer is clearly labeled.
- Third-party products are excluded from Scout cart objects.
- Scout does not present Scout’s return policy as the third-party retailer’s policy.

## Beat 5b — Authenticated Order and Shipment Tracking

Sign in as demo customer C001 first (via the sign-in flow), then ask:
"Where is order O1001?"

Expected demo point:

- Real, live shipment tracking (carrier, status, estimated delivery) for
  an order the signed-in customer actually owns.
- Mention (rather than demo live, for time): an unauthenticated request,
  or a request for a DIFFERENT customer's order, is blocked the same way -
  this is a real, tested security boundary, not just a happy-path demo.

## Beat 7 — Business Impact

Open `/admin/impact` in a new tab.

Expected demo point:

- Every recommendation shown earlier was tagged with a real
  `recommendation_id`.
- If a recommended item was added to cart and checked out during this
  demo, the dashboard's Scout-assisted revenue number reflects it live -
  calculated with a direct, deterministic SQL query, zero AI involvement
  in the number itself.
- This is the same kind of attribution real retail AI teams use
  internally to measure whether an AI assistant investment is working.

## Beat 6 — Payment Boundary

Ask: “Can you charge my card and buy this?”

Expected demo point:

- Agent refuses to charge/process payment directly.
- Checkout remains available through the secure storefront route.
- No checkout/payment/refund/cancel tool is exposed to specialists.

## Closing Line

“Anywhere a mistake would cost money or trust, that decision belongs to code, not the AI.”
