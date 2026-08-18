# Scout — 3-5 Minute Demo Script

Setup: backend running (`uvicorn scout.main:app --app-dir src`), frontend
running (`npm run dev`), database freshly seeded.

---

**1. Recommendation**
> Type: `Recommend a dress under $80`
Proves: real product search against real inventory, with live promotions applied — not a scripted answer.

**2. Contextual follow-up (ordinal selection)**
> Type: `I like the second one`
Proves: Scout tracks what was just shown and resolves natural references, not just exact product names.

**3. Clarification mid-decision**
> Type: `wait, is it available in medium first?`
Proves: an interruption doesn't lose the original context — Scout answers the new question about the *same* product, correctly, then the original offer still stands.

**4. Confirm the cart action**
> Type: `yes`
Proves: cart additions only happen on a real, explicit confirmation — the AI never adds anything unilaterally.

**5. Store availability**
> Type: `Is it available at Maple Grove?`
Proves: a second, different specialist agent (inventory) takes over seamlessly, checking real, store-specific stock.

**6. Authenticated order + shipment tracking**
*(Sign in as demo customer C001 first, via the sign-in flow)*
> Type: `Where is order O1001?`
Proves: real, authenticated order access with live shipment tracking (carrier, status, estimated delivery) — and that unauthenticated or cross-customer requests are blocked (mention this rather than demoing the failure live, for time).

**7. Policy question**
> Type: `Can I return an opened item?`
Proves: a fourth specialist (policy) answers from real policy documents, with a natural, helpful next step — not a generic FAQ dump.

**8. Honest fallback**
> Type: `Do you have any red cocktail dresses under $50?`
Proves: when Scout genuinely has nothing that matches, it says so honestly and offers real, clearly-labeled third-party alternatives — never fabricating a product.

**9. Checkout (deterministic, no AI)**
*(Complete checkout via the storefront UI, using the test card 4242 4242 4242 4242)*
Proves: checkout and payment are handled by ordinary, deterministic backend code — the AI has no ability to create orders or touch payment, by design.

**10. Business impact**
*(Open `/admin/impact`)*
Proves: the sale you just completed is now reflected in real, measured Scout-attributed revenue — closing the loop from recommendation to real, provable business value.

---

## One-sentence framing to open with

*"Scout is an agentic retail assistant that provides verified recommendations and customer support while keeping payments, checkout, and other sensitive commerce operations behind deterministic system boundaries — and it measures its own business impact and reliability, not just its ability to chat."*
