# Release Checklist — Scout Demo v1

## Validation

- [x] Backend deterministic tests: `411 passed`
- [x] Frontend lint: `0 errors`, `0 warnings`
- [x] Frontend build: passed
- [x] Local-only backend startup: `ENABLE_STRIPE_MCP=false MODEL_PROVIDER=ollama`
- [x] Full release evaluation: 23/23 safety and usefulness, 0 HTTP 500, 0 timeouts

## Artifacts

- [x] Release metrics summarized in `docs/evaluation.md`
- [ ] Decide whether generated live eval JSON/log artifacts should stay local or be committed as explicit release evidence.

## Demo Readiness

- [x] README explains architecture, setup, validation, and demo flow.
- [x] Architecture docs describe deterministic lane, agentic lane, evidence pipeline, and MCP boundary.
- [x] Security docs state what the agent cannot do.
- [x] Demo script includes recommendation, inventory, store, multi-intent, external fallback, and payment-boundary beats.
- [x] Observability docs explain structured diagnostics and local Ollama caveats.

## Before Public Sharing

- [ ] Add screenshots to `docs/screenshots/`.
- [ ] Add or choose a repository license.
- [x] Confirm no real `.env`, Stripe keys, provider keys, customer data, or secrets are staged. (Verified: `git ls-files | grep -iE "\.env$|stripe.*key|api.*key"` returns nothing.)
- [ ] If committing generated live eval artifacts, review them first for size and secret/log hygiene.
- [ ] If deploying, add PRODUCTION-GRADE auth (current order authorization fails closed correctly, but demo auth is local-only and not production identity/session infrastructure — see security.md), rate limiting, persistent session storage, and production payment/security review.
