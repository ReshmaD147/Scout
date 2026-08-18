# Scout — Attribution End-to-End Demo Script

Verified live on 2026-08-17. Every step below was run exactly as written
and produced the real results shown.

## Prerequisites

```bash
cd ~/Desktop/Scout/backend
uvicorn scout.main:app --reload --app-dir src
```

Confirm the database has real data:
```bash
curl "http://127.0.0.1:8000/products?limit=3"
```

## Step 0 — Baseline (optional, for a clean before/after comparison)

```bash
curl "http://127.0.0.1:8000/analytics/scout-attributed-revenue"
```
Expected: whatever the current, real cumulative total is (0 if freshly seeded).

## Step 1 — Get a real recommendation

```bash
curl -X POST http://127.0.0.1:8000/chat -H "Content-Type: application/json" \
  -d '{"message": "Recommend a dress under $80"}'
```

**What to point out:** each product in the response includes a real,
unique `recommendation_id` (e.g. `rec_a6e0f8e9382b`) - generated the
moment Scout's recommendation passes through the verification pipeline.
This is what makes attribution possible: the ID only exists because this
specific product was genuinely, verifiably recommended.

**Capture:** one product's `product_id` and `recommendation_id` from the response.

## Step 2 — Add it to cart, carrying the recommendation_id

```bash
curl -X POST http://127.0.0.1:8000/cart/add -H "Content-Type: application/json" \
  -d '{"product_id": "P003", "quantity": 1, "recommendation_id": "rec_a6e0f8e9382b"}'
```

**What to point out:** the response includes `"attribution_source":"scout"`
- but only because the `recommendation_id` was independently validated
against the real registry, not just trusted because the frontend sent it.
(Demonstrate the failure case too, if time allows: send a fake
`recommendation_id` and show `attribution_source` comes back `null`,
while the cart-add itself still succeeds.)

## Step 3 — Complete checkout

```bash
curl -X POST http://127.0.0.1:8000/checkout -H "Content-Type: application/json" \
  -d '{"items": [{"product_id": "P003", "quantity": 1, "attribution_source": "scout", "recommendation_id": "rec_a6e0f8e9382b"}]}'
```

**What to point out:** this is completely deterministic, non-AI code -
real stock re-validation, real order creation, a real Stripe payment
intent. Capture the returned `order_id`.

## Step 4 — Verify the real, persisted database record

```bash
python3 -c "
from scout.db.session import SessionLocal
from scout.db.models import OrderItem
session = SessionLocal()
items = session.query(OrderItem).filter_by(order_id='O444EB108').all()
for i in items:
    print(i.order_id, i.product_id, i.attribution_source, i.recommendation_id, i.price_at_purchase)
"
```
(Replace `O444EB108` with your actual order_id from Step 3.)

**What to point out:** this is a real database row - the attribution
survived from the AI's recommendation, through cart validation, through
checkout, into permanent storage.

## Step 5 — Confirm the business metric

```bash
curl "http://127.0.0.1:8000/analytics/scout-attributed-revenue"
```

**What to point out:** this number is calculated with pure, deterministic
SQL aggregation - zero AI involvement in the calculation itself. This is
the actual, provable answer to "did the AI assistant contribute to real
sales" - not a claim, a real, queryable number.

## The one-sentence summary to say while doing this

"Watch this number go from zero to a real dollar amount, purely by
following one real recommendation through cart, checkout, and into the
database - this is the complete proof that Scout doesn't just chat, it
measurably drives revenue, and every step of that chain is independently
verified, never just trusted."

## Real, confirmed results from this exact run

- Recommendation: Wrap Dress, `rec_a6e0f8e9382b`
- Order created: `O444EB108`, $68.00
- Database confirms: `attribution_source='scout'`
- Analytics after: `{"scout_assisted_revenue":68.0,"scout_assisted_orders":1,"scout_attributed_items":1}`
