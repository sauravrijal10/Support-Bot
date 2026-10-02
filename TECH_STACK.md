# Tech Stack — What We Use and Why

A short guide to each technology in the Support Bot, what it does for us, and its trade-offs. For how the pieces fit together, see [DOCUMENTATION.md](DOCUMENTATION.md).

## At a glance

| Need | Choice | Cost |
|---|---|---|
| Answer generation (LLM) | `Qwen/Qwen2.5-72B-Instruct` (pinned to the `deepinfra` provider) | Billed per token against HF credits |
| Agentic behaviour | Tool calling (OpenAI-style `tools`/`tool_calls`) | Included in the LLM cost above |
| LLM access | Hugging Face Inference Providers (`huggingface_hub`) | Billed per token; see below |
| Turning text into vectors | `BAAI/bge-small-en-v1.5` via `fastembed` | Free, runs locally |
| Vector search | PostgreSQL + `pgvector` | Free, self-hosted |
| Store data (products, orders, tickets, customers) | PostgreSQL via SQLAlchemy ORM | Free, self-hosted |
| Schema changes | Alembic migrations | Free |
| API server | FastAPI + Uvicorn (class-based views for store resources) | Free |
| Passwords / login | Argon2 + JWT | Free |
| Packaging | Docker + Docker Compose | Free |

Everything except the LLM is free and self-hosted. The LLM is **not** free: no
provider on Hugging Face's router serves any model at no cost (verified
against the live `https://router.huggingface.co/v1/models` listing), so
every chat request draws down the account's HF credits. Budget for that, and
watch for `402 Payment Required` from the API when credits run out.

---

## 1. LLM — `Qwen/Qwen2.5-72B-Instruct`

**What it does:** decides which tools to call (if any), reads their results plus the retrieved FAQ text, and writes the final answer. Configured via `LLM_MODEL`/`LLM_PROVIDER` and wrapped in `app/services/llm.py`.

**Why this model and not `phi-4` (the original choice):** tool calling needs the model to reliably return a structured `tool_calls` response instead of describing the call in prose. We tested this empirically rather than assuming it:
- `phi-4`'s only providers on the router either don't advertise `supports_tools` or explicitly report `false`. Calls with a `tools` list failed outright.
- `Qwen2.5-72B-Instruct` correctly chose the right tool (or no tool) on every test question, including ones needing a specific argument (`max_price`) or none at all.
- `Llama-3.1-8B-Instruct` was unreliable in the same tests: it invented a tool call with a fabricated order id for a question that needed no tool, and re-called a tool instead of answering after receiving its result. Smaller open models are markedly less trustworthy at this than a 72B one.

**Pros**
- **Open weights** — can be self-hosted later (e.g. via vLLM) if the hosted cost or reliability stops being acceptable.
- **Tool calling actually works**, verified against this project's real tool schemas rather than assumed from documentation.
- **Large enough to follow multi-step instructions** — "call a tool, then answer only from its result" is a harder instruction to follow than a single-shot RAG answer.
- **Swappable** — `LLM_MODEL`/`LLM_PROVIDER` env vars, not a code change.

**Cons**
- **Not free** — billed per token; a tool-calling conversation costs noticeably more than the old single-call design (see the loop cost note below).
- **Provider-dependent** — tool support varies by provider even for the same model id (`Qwen2.5-72B-Instruct` supports tools on `deepinfra`/`novita` specifically; pin the provider rather than use `"auto"`, or a request can silently land on one that doesn't).
- **Data leaves our infrastructure** — the question, tool results and customer context are all sent to a third-party provider. Matters more now than before, since tool results can include order and ticket contents.
- **Non-deterministic**, though we set `temperature=0.2` to make tool-argument choices and refusals more consistent.
- **Slower first token** (~3.8s observed on `deepinfra`) than the smaller `phi-4` was (~0.4s), before even counting the extra round-trips a tool call adds.

## 2. LLM access — Hugging Face Inference Providers

**What it does:** an OpenAI-style `chat.completions` API (including `tools`/`tool_calls`) in front of many hosted models, used through `huggingface_hub.InferenceClient`.

**Pros**
- One token, many models; switching models is an env var, not a code change.
- Familiar chat-completions and tool-calling interface — the same shapes as OpenAI's API.
- No GPU needed on our side.

**Cons**
- **Third-party routing** — requests go through HF to whichever provider serves the model, adding latency and a failure point.
- **Token permissions are fiddly** — needs the "Make calls to Inference Providers" scope (a missing scope caused our 403).
- **Not actually free**, despite frequent assumptions to the contrary (including an earlier version of this document) — every model+provider pair we found is billed.
- Tool support and pricing both vary by provider for the same model, so the model id alone doesn't tell you what you'll get.

**Alternatives we considered:** Ollama (fully local, no per-token cost, no rate limits — but needs local hardware, a model download, and tool-calling quality varies by locally-run model) and Groq (fast, but rate-limited on its free tier). Anthropic's API was replaced early on because the goal was open models, not because of tool-calling specifically.

## 3. Tool calling

**What it does:** instead of the server deciding up front what data to hand the model (the original design: always fetch the catalog, always fetch the signed-in customer's orders and tickets, paste it all into one prompt), the model is given tool schemas and decides per-question whether to call one, reads the result, and can call another before answering. See `app/services/tools.py` and `chat_service.answer_question`'s loop.

**Pros**
- **Handles phrasing the old regex/always-fetch approach couldn't** — "what did I buy last week?" (no "order" keyword) or "any plan under $10?" (a constraint the flat catalog dump couldn't apply) both resolve to the right tool call.
- **Cheaper per question when the tool isn't needed** — a pure FAQ question ("how do I reset my password?") never fetches the catalog or the customer's orders at all, unlike the old design which fetched the catalog on every single question regardless of relevance.
- **Scoping is structural, not conventional** — no tool's argument schema accepts a customer id; every account tool reads it from the verified login token. A signed-out caller is never even offered the account tools. This is enforced by what functions exist and what they're offered, not by a rule the model has to remember to follow.
- **Tested against a scripted fake model** (`tests/test_tool_calling.py`) rather than only against the real LLM, so the loop's safety properties (customer scoping, repeated-call and step-count guards, error handling, argument validation) are pinned by fast, free, deterministic tests instead of relying on the live model behaving each time.

**Cons**
- **More expensive per question when a tool IS needed** — each round-trip resends the full message history plus every tool schema; a two-call conversation is several times the tokens of the old single-call design.
- **Needs a model that supports it reliably** — this is what forced the `phi-4` → `Qwen2.5-72B` switch (see §1); tool-calling quality is not a given for any particular open model.
- **A new failure surface** — malformed arguments, a model calling the same tool repeatedly, or refusing to stop calling tools. Guarded here with argument validation (invalid input becomes a tool-result error the model reads, not a crash), a repeated-call cache, a per-step call cap, and a hard cap on tool-calling rounds that forces a final tools-off answer — all covered by tests.
- **Tool results are untrusted data that reaches the model** — a ticket description is customer-written text that could contain "ignore your instructions"; the system prompt tells the model to treat tool results as data, not instructions, but that is a prompt-level mitigation, not a hard guarantee.
- **The model doesn't always use a tool even when one exists** — in testing, a product question that happened to also be answerable from the retrieved FAQ text was answered from the FAQ without calling `search_products`, even though the tool was offered. The FAQ retrieval step (kept for policy/how-to answers) can give the model an escape hatch from calling a tool for data that should really come from the live table.

## 4. Embeddings — `BAAI/bge-small-en-v1.5` via `fastembed`

**What it does:** converts FAQ chunks and customer questions into 384-number vectors so we can find text by *meaning* rather than exact words.

**Pros**
- **Runs locally** — no API calls, no rate limits, no provider dependency, works offline.
- **Small and fast** — ONNX runtime, CPU-only, no PyTorch (much lighter than `sentence-transformers`). Baked into the Docker image at build time.
- **Free.**
- Good enough to match paraphrases ("will my stuff get wiped if I stop paying?" finds the cancellation FAQ).

**Cons**
- **English-focused** — poor fit for multilingual customers.
- **Small model = blunt ranking** — we measured similarity scores clustering in a narrow 0.5–0.7 band, so the best match can rank 2nd–3rd (still caught at top-3).
- **512-token input limit** — long documents must be chunked.
- **Changing the model means re-indexing** — the `documents` table is fixed at 384 dimensions.
- Adds ~100+ MB to the image and a one-time model load at startup.

## 5. Vector search — PostgreSQL + `pgvector`

**What it does:** stores the FAQ embeddings and finds the closest ones to a question (cosine similarity, HNSW index).

**Pros**
- **One database for everything** — vectors and customer records together; no extra service to run or secure.
- Simple to operate and back up; standard SQL.
- Plenty fast for thousands of chunks.

**Cons**
- **Not built for huge scale** — dedicated vector databases (Pinecone, Qdrant, Weaviate) do better at millions of vectors and offer more search features.
- Needs the `pgvector/pgvector` image, not stock Postgres.
- HNSW indexes use memory and slow down bulk writes.
- We currently **wipe and re-embed everything on every startup** — trivial for 8 chunks, wasteful for a large knowledge base.

## 6. Store data — PostgreSQL + SQLAlchemy + Alembic

**What it does:** stores customers, products, orders (with line items), tickets and password hashes. The REST API reads and writes it; the chatbot reads it, by exact key, on every question.

**Pros**
- **SQLAlchemy 2.0 models** are one definition of each table (types, relationships, CHECK constraints) that both the API and the chatbot's data helpers share.
- **Alembic migrations** give versioned, reviewable schema changes, and here converted real legacy data (old free-text orders → line items) without loss. `alembic check` catches model/database drift.
- **Postgres guarantees the hard parts:** transactions (an order and its stock change succeed or fail together), row locks (8 simultaneous buyers of the last item → exactly one wins), foreign keys, uniqueness and CHECK constraints (no negative price/stock even if app code has a bug).
- **A pooled engine** reuses connections instead of opening one per query.
- Same server as the vector store.

**Cons**
- **More moving parts** than raw SQL: an ORM to learn, and migrations to write and review (autogenerate isn't always right — always read the file).
- **Migrations run at app startup** here for convenience, which can race with several app instances; they'd need to become a separate deploy step.
- **Isolation between customers lives in application code** (every query filters by `customer_id`); Row-Level Security would add a second safeguard.
- **Sync ORM** — calls run in FastAPI's thread pool rather than truly async.
- The pgvector store still uses its own raw connections, so there are two data-access styles in the codebase.

**Design choice worth knowing:** the chatbot never gets database access or writes SQL. Fixed, parameterised queries pick the rows and the model only reads the resulting text. Letting an LLM generate SQL would give anyone who can chat a route to the whole database.

## 7. API server — FastAPI + Uvicorn

**What it does:** exposes `/chat` and `/auth/*`, validates requests, and generates the Swagger docs.

**Pros**
- Automatic request validation (Pydantic) and interactive docs at `/docs`.
- Clean dependency system — how authentication, admin checks and the DB session plug in.
- Fast, modern, widely used.
- **Class-based views** (products, orders, tickets) group each resource's endpoints in one class, which reads well for CRUD-style resources.

**Cons**
- **FastAPI has no built-in class-based views.** We wrote a small helper (`app/api/views/base.py`, ~40 lines) rather than depend on a third-party package; it's ours to maintain. Function-based routes remain for auth/chat/health, so the codebase has two styles.
- Our database and model calls are blocking, so they run in a thread pool rather than truly async.
- Currently a single dev-mode process (`--reload`); production needs multiple workers and no reload.

## 8. Authentication — Argon2 + JWT

**What it does:** argon2id hashes passwords; login returns a signed JWT that identifies the customer on later requests.

**Pros**
- **Roles:** a simple `is_admin` flag gates product writes and order/ticket status changes. Easy to reason about, but coarse (no finer permissions) and no admin-management or audit tooling yet.
- **Argon2id** is a modern, memory-hard hash — much costlier to brute-force than older hashes.
- **JWT is stateless** — no session storage; the API just verifies the signature.
- Standard OAuth2 flow, so Swagger's Authorize button works.

**Cons**
- **No revocation** — a stolen token works until it expires.
- **One shared secret (HS256)** — anyone who has it can forge tokens; rotating it logs everyone out.
- **No rate limiting on login yet**, and HTTP-only in dev (needs TLS in production).

## 9. Retrieval design choices

| Choice | Pros | Cons |
|---|---|---|
| **RAG** (retrieve, then generate) instead of fine-tuning | Cheap; update answers by editing `faq.md`; answers are traceable to sources | Quality depends on retrieval finding the right chunk |
| **Chunk by `##` heading** | One FAQ entry per chunk keeps answers focused | Relies on well-structured docs; very long sections aren't split |
| **Regex to spot account-type questions from signed-out callers** | Deterministic, instant, no LLM cost | Brittle — misses unusual phrasing (mitigated: the account tools simply aren't offered to a signed-out caller, so a missed phrase can't leak data, only give a less helpful answer) |
| **Tool calling for account/product lookups, not a regex-selected DB query** | The model chooses what to fetch based on the actual question, not a keyword match | Needs a tool-capable model; more round-trips and tokens than a fixed lookup |
| **Exact DB lookup inside each tool handler, similarity search for FAQs** | Each data type gets the right mechanism; private data never enters similarity search | Two code paths (tools vs. vector search) to maintain |

## 10. Packaging — Docker + Docker Compose

**Pros**
- Reproducible setup: one command starts the API and a pgvector-enabled Postgres.
- No local Postgres installation or root access needed.

**Cons**
- Config is **dev-oriented** (bind mount, `--reload`, hardcoded DB password) and must be hardened for production.
- Host ports can clash with other containers (we hit this on 5433 and 8000).

---

## Biggest trade-offs to know about

1. **The LLM costs real money per question, and tool calling multiplies that.** Budget for it, watch for `402`s, and consider self-hosting (Ollama/vLLM) if volume or data privacy make a hosted provider unworkable.
2. **Small embedding model = adequate, not sharp.** Upgrade to `bge-base` or add a reranker if answer quality slips as the FAQ grows.
3. **The model can skip a tool when the FAQ happens to overlap.** If you need database data to always win over static text for a given topic, that topic may need to be pulled from `faq.md` so the tool is the only source, not an optional one.
4. **Everything single-instance and unpooled.** Fine for a demo; connection pooling, TLS and login rate limiting come first before real users.
