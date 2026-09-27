# Example run

A real, unedited run exported from the app's own database. Nothing here is
mocked or cleaned up.

**What to notice is not that the answers are good -- it is that they disagree.**
Four models given an identical prompt return four similar answers, and merging
those produces mush. So each seat is pushed toward a conflicting angle and sits
on a different vendor. The Pragmatist optimises for shipping this week; the
Contrarian assumes the obvious approach is wrong and attacks the premise.

---

## The question

> How should the backend handle a mid-debate model failure-like the Critic timing out or failing-and seamlessly failover to a backup model without dropping the live SSE stream or losing context state?

---

## What each model proposed, in parallel

### Pragmatist
`groq · openai/gpt-oss-120b`

**Ship‑today solution: “Model Proxy + SSE keep‑alive”**

1. **Core stack (week‑ready, proven)**
   - **FastAPI** (Uvicorn) – async HTTP endpoint for SSE (`GET /stream/{sid}`) and control (`POST /action`).
   - **Redis** (single node or managed) – per‑session `list` for context, `pub/sub` queue for token chunks, and a tiny hash for circuit‑breaker state.
   - **Python wrapper (`model_proxy.py`)** – sync/async wrapper around any OpenAI‑compatible endpoint (cloud or local Ollama/vLLM). No extra orchestration layer.
   - **Background worker** – simple `asyncio.create_task` per session that pulls from Redis queue and pushes to SSE generator.

2. **Streaming pipeline (no drop)**
   ```text
   Frontend <--SSE--> FastAPI endpoint
        ↕                     |
   asyncio.Queue (session)   |
        ↕                     |
   model_proxy → provider A (primary) → token chunks → Redis PUB/SUB → Qu

*[...truncated]*

---

### Systems Thinker
`gemini · gemini-3.8-flash`

Decouple the client-facing SSE pipe from the LLM execution lifecycle using **Attempt-Scoped Stream Framing** backed by an ephemeral Redis Stream buffer. The API Gateway holds the HTTP connection open indefinitely; the worker executing the model call is completely disposable.

### The Mechanism

Do not stream un-scoped raw text to the client. If Model A streams 40 tokens and crashes, Model B cannot seamlessly complete Model A’s half-sentence without semantic dissonance. 

Instead, model generation must be framed as an **Attempt** within an **Agent Turn**:

```
[Agent Turn: Round 2, Critic]
   ├── Attempt 0 (Claude-3.5-Sonnet) ──► [FAIL: Timeout @ 3s] ──► Emit: {type: "attempt_failed", rollback: true}
   └── Attempt 1 (DeepSeek-V3-Local)   ──► [SUCCESS]           ──► Emit: {type: "delta", ...} -> {type: "commit"}
```

#### Data Flow & State Hand-off
1. **The Gateway Isolation:** Fastify/Go

*[...truncated]*

---

### Contrarian
`cerebras · qwen-3.8-27b`

Do not make failover transparent. In a debate, the Critic’s model is part of the argument. The hidden false assumption is that “seamless” means invisible. It should mean *continuous liveness*: visibly hand the Crit

---

### Operator
`gemini · gemini-3.5-flash`

**Recommendation:** Use an **Active-Progressive Fallover** pattern executed inside the Python Orchestrator. When a model fails, immediately yield a system-level "meta-event" to the SSE stream to reset client-side idle timers, swap the client-side provider configuration in-memory, and hot-resume the prompt generation with a aggressive 6.0s timeout. 

Here is how this works under the hood, and what will break in production:

### The Operational Reality (The 2 AM Problems)

1. **Proxy Dropouts (The $0/mo Silent Failure):** If a model hangs and your orchestrator waits for a 30-second default SDK timeout, Cloudflare or Nginx will kill the SSE connection (default `proxy_read_timeout` is often 60s, but Cloudflare drops idle HTTP connections at 100s, and mobile clients drop much faster).
2. **The "Ghost Model" Audit Trail:** When debugging a bad debate output tomorrow, you won't know if the Crit

*[...truncated]*

---

## The merged answer

One model reads every proposal above, resolves the contradictions rather than
averaging them, and closes with the decisions the user still has to make --
which is the part the whole tool exists for.

### 1. The Recommendation
Do not attempt mid-sentence text continuation between different LLMs, and do not bind the frontend Server-Sent Events (SSE) connection directly to an upstream model API call. Instead, isolate the SSE socket at the API gateway reading from an ephemeral Redis Stream, treat each agent response as an **attempt-scoped draft**, and if a provider stalls past a strict Time-To-First-Token (TTFT) deadline, emit an explicit `attempt_reset` event to purge the frontend's uncommitted buffer and restart that specific agent's turn cleanly against the fallback model from an immutable context snapshot.

---

### 2. Implementation Architecture & Substance

#### A. Decoupled Pipeline & Generation Isolation
The client-facing SSE stream must never touch the Python LLM worker process directly. If a worker times out or crashes, the HTTP stream must remain completely unaware.

```
[ Frontend ] <=== SSE (/stream/{sid}) === [ Edge Gateway (Go/Fastify) ]
                                                    ▲
                                            XREAD BLOCK (Redis Stream)
                                                    │
[ Orchestrator Worker ] ── Writes Events ──► [ session:{id}:events ]
        │
        ├── Context Store: Immutable Snapshot at Round Start (`session:{id}:round_{n}:snap`)
        └── Lock/Nonce: `session:{id}:active_nonce` (prevents ghost generation races)
```

1. **Gateway Holds the Pipe:** The gateway holds the long-lived HTTP connection and reads from `session:{id}:events` via Redis `XREAD BLOCK`. It emits a lightweight heartbeat (`{"type": "heartbeat"}`) every 10 seconds if no events occur, preventing Cloudflare/Nginx `proxy_read_timeout` drops.
2. **Workers Are Disposable:** The orchestrator worker pulls from a queue, processes the turn, an

*[...truncated]*

---

*Generated by [Council AI](https://github.com/abu4evere/council). This run used
four proposers; a fifth (Skeptic, on Kimi) was added later.*