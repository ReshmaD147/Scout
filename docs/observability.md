# Observability

Scout emits structured internal diagnostics for latency and safety debugging. Logs intentionally avoid prompts, raw histories, raw tool responses, evidence contents, API keys, credentials, environment variable values, and chain-of-thought.

## Diagnostic Events

The backend logs `scout_diagnostics` JSON records for stages such as:

- request start and completion
- deterministic intent classification
- intent splitting
- direct routing or Supervisor graph invocation
- specialist selection
- model invocation duration
- tool call name and duration
- embedding generation
- Chroma product/policy retrieval
- evidence early completion
- claim proposal
- claim verification
- approved-only rendering
- targeted correction start/completion
- timeout/cancellation cleanup

## Release Artifact Summary

When regenerated locally, `backend/tests/eval/results/scout-demo-v1/diagnostics_summary.json` maps the release run’s 27 HTTP requests to scenario IDs and summarizes:

- model calls and durations
- tool calls and tool names
- selected specialists
- direct route vs Supervisor route
- evidence early completion
- deterministic tool-first execution
- corrections and timeouts
- model calls avoided
- slowest stages

## Release Metrics

**Snapshot from July 30, before this week's agent-layer refactor and
the ACCESS_DENIED fix — re-run the eval suite and regenerate
`diagnostics_summary.json` for current numbers.**

- Total model calls: 16
- Total tool calls: 42
- Model calls avoided: 17
- Deterministic no-model responses: 11
- Tool-first executions: 7
- Corrections: 2
- Timeouts: 0
- HTTP 500s: 0

## Reading Logs Safely

Use logs for timing and stage sequencing, not for reconstructing customer prompts or secrets. Keep generated `server.log` files local unless they have been explicitly reviewed for size and secret/log hygiene; do not add logs that include real credentials, `.env` contents, raw tool payloads, or full conversation histories.

## Practical Demo Advice

Ollama is local and serializes/queues work on many machines. For a smooth demo:

- Run evaluation and manual demos separately.
- Keep `ENABLE_STRIPE_MCP=false` unless explicitly testing Stripe MCP discovery.
- Keep `qwen3:8b` and `nomic-embed-text` warm before demoing.
- Watch for slow order/inventory model paths; deterministic multi-intent paths are much faster.
