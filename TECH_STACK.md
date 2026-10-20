# Tech Stack — What We Use, Why It Matters, and Why We Chose It

This document explains every tool and technology in the Digi Store support
platform: **what it does in the system**, **why it is significant**, and
**why it was chosen over the alternatives**. Where a choice was made after a
test or a real problem during development, that is stated, so the reasoning
can be checked rather than taken on trust.

For how the code is organised and how to run it, see [README.md](README.md);
for the build history, see [DOCUMENTATION.md](DOCUMENTATION.md).

---

## Contents

1. [The system in one picture](#1-the-system-in-one-picture)
2. [At a glance](#2-at-a-glance)
3. [Language — Python 3.12](#3-language--python-312)
4. [API server — FastAPI, Uvicorn, Pydantic](#4-api-server--fastapi-uvicorn-pydantic)
5. [Real-time chat — WebSockets](#5-real-time-chat--websockets)
6. [Web chat interface — plain HTML and JavaScript](#6-web-chat-interface--plain-html-and-javascript)
7. [The language model — Qwen2.5-72B-Instruct](#7-the-language-model--qwen25-72b-instruct)
8. [LLM access — Hugging Face Inference Providers](#8-llm-access--hugging-face-inference-providers)
9. [Agent design — tool calling with a LangChain agent](#9-agent-design--tool-calling-with-a-langchain-agent)
10. [Safe write actions — quote, then confirm](#10-safe-write-actions--quote-then-confirm)
11. [Knowledge retrieval (RAG) — fastembed + bge-small](#11-knowledge-retrieval-rag--fastembed--bge-small)
12. [Vector search — pgvector](#12-vector-search--pgvector)
13. [Database — PostgreSQL](#13-database--postgresql)
14. [Flexible catalog data — JSONB](#14-flexible-catalog-data--jsonb)
15. [Data access — SQLAlchemy 2.0 + psycopg2](#15-data-access--sqlalchemy-20--psycopg2)
16. [Schema changes — Alembic](#16-schema-changes--alembic)
17. [Authentication — Argon2 + JWT](#17-authentication--argon2--jwt)
18. [Testing — pytest, fakes and a real database](#18-testing--pytest-fakes-and-a-real-database)
19. [Packaging — Docker and Docker Compose](#19-packaging--docker-and-docker-compose)
20. [Configuration — environment variables](#20-configuration--environment-variables)
21. [Planned — WhatsApp](#21-planned--whatsapp)
22. [The biggest trade-offs, and when to revisit them](#22-the-biggest-trade-offs-and-when-to-revisit-them)

---

## 1. The system in one picture

```
 Browser chat page ──REST (POST /chat)──┐
 (app/web/index.html) ──WebSocket──────┤
                                        ▼
                         FastAPI app (Uvicorn)
          ┌──────────────┬──────────────┼───────────────┬──────────────┐
          ▼              ▼              ▼               ▼              ▼
     Auth (JWT,     REST resources   Chat service     FAQ search      Order form
     Argon2)        products, types, (tool-calling    (fastembed →    confirm /
                    orders, tickets  loop)            pgvector)       discard
                          │              │                 │
                          │      ┌───────┴────────┐        │
                          │      ▼                ▼        │
                          │  Hugging Face     Tools: search,│
                          │  Inference        orders, tickets,
                          │  (Qwen2.5-72B)    quote/confirm  │
                          ▼                       ▼        ▼
                    ┌──────────────── PostgreSQL ──────────────────┐
                    │ relational tables · JSONB attributes &       │
                    │ variants · pgvector FAQ embeddings           │
                    └──────────────────────────────────────────────┘
```

A customer's message reaches the chat service over REST or a WebSocket. The
service fetches the most relevant FAQ sections, then lets the language model
call a fixed set of tools (search products, look up the customer's own orders,
prepare or confirm an order...) until it can answer. Everything — customers,
catalog, orders, FAQ vectors — lives in one PostgreSQL database.

---

## 2. At a glance

| Layer | Technology | Role in the system | Main alternatives considered |
|---|---|---|---|
| Language | Python 3.12 | All server code | Node.js/TypeScript, Go |
| API | FastAPI + Uvicorn + Pydantic | REST endpoints, WebSocket, validation, `/docs` | Django REST Framework, Flask, Express |
| Real-time | WebSockets (built into FastAPI/Starlette) | Stateful chat conversations | Server-Sent Events, long polling, Socket.IO |
| Web UI | One HTML file, plain JavaScript | Chat page + order form | React/Vue/Next.js |
| LLM | `Qwen/Qwen2.5-72B-Instruct` on `deepinfra` | Understands questions, picks tools, writes answers | `microsoft/phi-4`, Llama-3.1-8B, GPT/Claude APIs |
| LLM access | Hugging Face Inference Providers (`huggingface_hub`) + multi-token failover | Hosted model API | OpenAI API, self-hosting (vLLM/Ollama), Groq |
| Agent | LangChain `create_agent` (LangGraph) with our tools, guards as middleware | Lets the model fetch live data and act safely | Hand-written loop (the earlier design), LlamaIndex, prompt stuffing, LLM-written SQL |
| Embeddings | `BAAI/bge-small-en-v1.5` via `fastembed` | Turns FAQ text and questions into vectors | sentence-transformers, OpenAI embeddings, bge-base |
| Vector search | `pgvector` (HNSW, cosine) | Finds FAQ sections by meaning | Pinecone, Qdrant, Weaviate, Chroma, FAISS |
| Database | PostgreSQL 16 | All data; transactions and locks | MySQL, MongoDB, SQLite |
| Flexible data | JSONB columns | Per-type attributes, product variants, order drafts | Extra tables (EAV / variants table), MongoDB |
| Data access | SQLAlchemy 2.0 ORM + psycopg2 | Models, queries, sessions | Raw SQL, Django ORM, SQLModel |
| Migrations | Alembic | Versioned schema changes (0001–0006) | `create_all()`, hand-run SQL scripts |
| Auth | Argon2id (`argon2-cffi`) + JWT (`PyJWT`) | Passwords and login tokens | bcrypt, server sessions, Keycloak/Auth0 |
| Tests | pytest, fake LLM, rolled-back Postgres | 200+ automated tests | Mocks only, live-LLM tests |
| Packaging | Docker + Docker Compose (`pgvector/pgvector:pg16`) | One-command setup | Local installs, Kubernetes |
| Config | Environment variables + `python-dotenv` | Secrets and settings | Config files, secret managers |

**Cost:** everything is free and self-hosted **except the language model**,
which is billed per token against Hugging Face credits. No provider on the
Hugging Face router serves any model for free (checked against the live
`https://router.huggingface.co/v1/models` listing); a `402 Payment Required`
means the account is out of credit.

---

## 3. Language — Python 3.12

**Role.** Every part of the server — API, chat logic, tools, database code,
seed script, tests — is Python.

**Why it matters.** The chat features depend on AI libraries
(`huggingface_hub` for the model, `fastembed` for embeddings, `pgvector` for
vector types), and those are written for Python first.

**Why Python over the alternatives**

| Alternative | Why not |
|---|---|
| Node.js / TypeScript | Good for web servers, but the embedding and model libraries we use are Python-first; we would be calling them through wrappers or HTTP. |
| Go | Fast and simple to deploy, but a much thinner AI ecosystem; local embeddings would mean extra services. |

**Trade-off.** Python is slower per request than Go. That is irrelevant here:
a chat answer spends seconds waiting on the model, milliseconds in our code.

---

## 4. API server — FastAPI, Uvicorn, Pydantic

**Role.** FastAPI defines every endpoint: `/auth/*`, `/chat`, `/ws/chat`,
`/products`, `/product-types`, `/orders` (including the order-form
`/orders/drafts/{id}/confirm`), `/tickets`, `/health`, and the chat page at
`/`. Uvicorn is the server process that runs it. Pydantic models in
`app/schemas/` validate every request and response.

**Why it matters.**
- **Validation is the first security layer.** Every body is checked before
  our code runs, and every request schema rejects unknown fields
  (`extra="forbid"`), so a client cannot slip in `price`, `customer_id` or
  `is_admin`.
- **Dependency injection** is how authentication works: an endpoint declares
  `Depends(get_current_customer)` or `Depends(require_admin)` and FastAPI
  enforces it.
- **Automatic docs** at `/docs` (Swagger UI) with a working *Authorize* button,
  because login follows the standard OAuth2 password flow.
- **WebSockets come built in** (from Starlette, which FastAPI is built on), so
  the real-time chat needed no extra library.

**Why FastAPI over the alternatives**

| Alternative | Why not |
|---|---|
| Django + Django REST Framework | Batteries included (admin, ORM), but heavier, and its ORM would replace SQLAlchemy and Alembic. WebSockets need Django Channels as an extra layer. |
| Flask | Minimal and familiar, but validation, docs, async and WebSockets all need separate add-ons. |
| Express (Node.js) | Ruled out with the language choice (§3). |

**Trade-offs and how we handled them.**
- FastAPI has **no class-based views**, which suit CRUD resources (products,
  orders...). We wrote a ~40-line helper (`app/api/views/base.py`) instead of
  adding a third-party package. Function-based routes remain for auth, chat
  and health, so the code has two styles.
- Our database and model calls are **blocking**. Route functions are plain
  `def`, so FastAPI runs them in a thread pool; the WebSocket handler uses
  `run_in_threadpool` for the same reason. Fine at this scale; truly async
  code would serve more concurrent users per process.

---

## 5. Real-time chat — WebSockets

**Role.** `/ws/chat` keeps one connection per conversation
(`app/api/routes/chat_ws.py`). The client sends JSON frames (`auth`,
`message`, `reset`, `ping`); the server replies with `typing`, `answer`
(including any order form) or a coded `error`.

**Why it matters.**
- **The server keeps the conversation.** The client sends only the new
  question; follow-ups like "how many are in stock?" are understood from the
  earlier turns.
- **A "typing" signal** while the model works, so the user sees progress.
- **Security built into the protocol:** the login token is sent inside a
  message, not in the URL (URLs end up in access logs); it is re-checked
  before every question, so an expired token stops working mid-conversation;
  and signing in as a different customer clears the history, so one
  customer's answers never become context for another.

**Why WebSockets over the alternatives**

| Alternative | Why not |
|---|---|
| Server-Sent Events | One-way (server → browser). Good for streaming an answer, but the client would still need separate HTTP calls to send messages and authenticate. |
| Long polling | Works everywhere, but wasteful and more complex to get right. |
| Socket.IO | Adds rooms and automatic fallbacks we don't need, and requires its own client and server libraries instead of the standard browser `WebSocket`. |

**Trade-off.** The conversation history lives **in the server's memory**: a
restart or a dropped connection starts a fresh conversation. That's why the
REST endpoint also exists — `POST /chat` is stateless and accepts the
history from the client. Moving history into Postgres is planned for the
WhatsApp channel, where conversations span days.

---

## 6. Web chat interface — plain HTML and JavaScript

**Role.** `app/web/index.html`, served at `/`, is the customer chat page:
sign in or register, chat over WebSocket or REST (switchable in the header),
see which sources and tools each answer used, and fill in the **order form**
(pick a variant, enter a quantity, watch the total update, confirm).

**Why it matters.** It is a working example client that uses only the public
API, so it doubles as living documentation of how to integrate.

**Why plain HTML/JS over a framework**

| Alternative | Why not (for now) |
|---|---|
| React / Vue / Svelte | Need a build step, `node_modules` and a separate dev server for what is one screen. |
| Next.js | Server rendering and routing a single chat page doesn't need. |

**Trade-off.** One ~630-line file is easy to read today; a larger customer
storefront (catalog pages, cart, account area) would justify a framework.
The page follows the system's light/dark preference and works at phone width.

---

## 7. The language model — Qwen2.5-72B-Instruct

**Role.** Reads the customer's question, the relevant FAQ sections and the
conversation so far; decides whether to call a tool (and with what
arguments); reads the results; writes the answer. Configured by
`LLM_MODEL` / `LLM_PROVIDER`, called through `app/services/llm.py`, with
`temperature=0.2` for consistent tool choices.

**Why it matters.** Every feature that isn't a plain REST call — product
search by description, "what did I order?", ordering and cancelling by chat
— depends on the model choosing the right tool and arguments.

**Why this model — chosen by testing, not assumption**

| Model | What happened in our tests |
|---|---|
| `microsoft/phi-4` (the original choice) | No provider on the router supports tool calling for it; requests with tools failed outright. |
| `meta-llama/Llama-3.1-8B-Instruct` | Unreliable: invented a tool call with a made-up order number for a question that needed no tool, and re-called a tool instead of answering. |
| **`Qwen/Qwen2.5-72B-Instruct`** | Chose the right tool, or no tool, on every test question, including ones needing a specific argument. |
| GPT-4o / Claude (proprietary APIs) | Excellent at tool calling, but the project's goal was an open-weights model that could later be self-hosted. |

**Other reasons.** Open weights (can move to our own GPUs via vLLM if cost or
privacy demands it); large enough to follow multi-step instructions ("quote
first, confirm only after the customer says yes"); swappable with an
environment variable.

**Trade-offs.**
- **Costs money per token**, and tool calling multiplies it (each round-trip
  resends the conversation and tool definitions).
- **Tool support depends on the provider**, even for the same model — hence
  the provider is pinned to `deepinfra` rather than `auto`.
- **Customer data leaves our infrastructure** — questions and tool results
  (including order contents) go to the provider.
- **Behaviour is steered, not guaranteed.** For example, the model at first
  asked "which size?" instead of showing the order form; a worked example in
  the prompt fixed most cases. Anything safety-relevant is therefore enforced
  in code, not left to the prompt (§9, §10).

---

## 8. LLM access — Hugging Face Inference Providers

**Role.** An OpenAI-style chat-completions API (with `tools` / `tool_calls`)
in front of many hosted models, used through `huggingface_hub.InferenceClient`.

**Why it matters.** It is the only external dependency the chat needs, so its
failures become our failures. Two things in `llm.py` handle that:
- **Clean failure.** One retry on transient errors (5xx, network); never on
  rate limits; every provider failure becomes a 503 "temporarily
  unavailable", with the real cause (e.g. `402` = out of credit) written to
  the server log.
- **Multiple tokens with failover.** `HF_TOKEN` plus `HF_FALLBACK_TOKENS`: if
  a token is expired (401), out of credit (402), not permitted (403) or
  rate-limited (429), the same request is retried on the next token, and the
  failed one is skipped for an hour (a minute for rate limits). This was
  added after the first account's credits ran out mid-testing.

**Why Hugging Face over the alternatives**

| Alternative | Why not (for now) |
|---|---|
| OpenAI / Anthropic APIs | Proprietary models only — against the open-weights goal. |
| Self-hosting (vLLM, Ollama) | No per-token cost and data stays in-house, but a 72B model needs serious GPU hardware. The right next step if volume or privacy grows. |
| Groq | Very fast, but free tier is rate-limited and model choice is narrower. |

**Trade-offs.** Not free (despite the "Inference Providers" branding);
requests pass through a router, adding latency and a failure point; tokens
need the "Make calls to Inference Providers" permission (a missing permission
caused 403s during development).

---

## 9. Agent design — tool calling with a LangChain agent

**Role.** Instead of pasting all data into the prompt, the model is offered
**tools** (`app/services/tools.py`). A LangChain agent (`create_agent`, a
LangGraph graph, built in `app/services/agent.py`) runs the loop: the model
asks for a tool, the tool runs, the result goes back, and it repeats (at most
4 rounds, 3 calls per round) until it answers.

**How LangChain is used.**
- **The loop** is LangChain's agent graph. The limits are agent middleware:
  `StepLimit` (after 4 tool rounds, one final call with no tools offered) and
  `ToolCallGuard` (calls beyond 3 per round, and exact repeats, get a result
  without running; tools the caller wasn't offered get a refusal).
- **The tools** are LangChain `StructuredTool`s, but each carries our own JSON
  schema and hands the model's raw arguments to `tools.run_tool`. So
  validation, the login re-check, error handling and size caps are still our
  audited code, not LangChain's.
- **The model** is our `LLMClient` wrapped as a LangChain chat model
  (`ClientChatModel`), keeping token rotation, provider fallbacks, retries,
  the 503 mapping, Gemini's `thought_signature` and streaming. LangChain's
  provider packages (`langchain-openai`, `langchain-google-genai`...) could
  replace it later.
- An agent is built per request (about 5 ms), because its tools are bound to
  that request's authenticated customer.

| Tool | Signed out? | What it does |
|---|---|---|
| `search_products` | Yes | Live catalog search by keywords, category, price, stock; shows variants |
| `list_product_types` | Yes | The store's categories |
| `get_order`, `list_my_orders`, `list_my_tickets` | No | The signed-in customer's own records |
| `prepare_order` / `confirm_order` | No | Quote, then place an order (§10) |
| `request_cancellation` / `confirm_cancellation` | No | Check, then cancel an order (§10) |

**Why it matters — security is structural, not a prompt rule.**
- **The model chooses *what* to look up, never *whose* data.** No tool takes a
  customer id; account tools read the customer from the verified login token.
  A test checks that no tool's parameters even mention "customer".
- **Signed-out users are never offered account tools**, and the tool runner
  re-checks this even if the model names one anyway.
- **Another customer's order looks exactly like a nonexistent one**, so order
  numbers can't be probed.
- **Arguments are validated** (a bad argument becomes an error the model reads,
  not a crash), results are size-capped, and tool output is treated as data,
  not instructions.

**Why this design over the alternatives**

| Alternative | Why not |
|---|---|
| Prompt stuffing (the original design: always paste the catalog and the customer's orders into the prompt) | Costly on every question, couldn't apply filters like "under $10", and couldn't take actions. |
| **Letting the model write SQL** | Rejected outright: anyone who can chat would have a route to the whole database. |
| Hand-written loop (used until this change) | About 100 lines and fully transparent, but every new behaviour (routing, multi-agent flows, tracing, retries) had to be built by hand. The LangChain agent keeps the same security checks in our own code while opening those up. The cost is a large, fast-moving dependency (`langchain` 1.x, `langgraph`). |
| LlamaIndex | Strongest at document retrieval pipelines; our retrieval is a single pgvector query, and LangChain's agent tooling fits the tool-calling core better. |

**Trade-offs.** More tokens per question when tools are used; needs a model
that calls tools reliably (§7); the model occasionally answers from the FAQ
instead of calling a tool when both seem to fit — which is why prices and
stock were deliberately left out of the FAQ.

---

## 10. Safe write actions — quote, then confirm

**Role.** Ordering and cancelling by chat are the only things the bot can
change. Both are two-step, with the state stored in Postgres (`order_drafts`,
`cancellation_requests`) so it works over REST, WebSocket and (later)
WhatsApp alike.

1. **Prepare** — `prepare_order` checks products, variants, stock and prices
   and saves a quote; `request_cancellation` checks the order is the
   customer's own and still pending. Nothing changes yet.
2. **Confirm** — happens only if the server verifies that:
   - the quote/request was made in an **earlier message** (quote and confirm
     can't happen in one turn);
   - the customer's **current message is a clear yes** (a strict pattern
     check: "no", "wait", "change"... don't count);
   - it's **under 15 minutes old**;
   - for orders, the **total hasn't changed**.

In the web chat, the quote also appears as an **order form**. Clicking
*Confirm* is itself the confirmation, and the page sends the total it showed:
if prices changed, nothing is ordered — the customer never pays a total they
didn't see.

**Why it matters.** A misunderstood "sure", or text hidden in a product
description telling the model to "order 50 of these", cannot place or cancel
an order. This is enforced by code and covered by tests against the real
database.

**Why this over the alternatives**

| Alternative | Why not |
|---|---|
| Let the model decide when the customer has confirmed | One misread message places an order; prompt rules aren't guarantees. |
| Confirmation only in the browser | Wouldn't work for text-only channels like WhatsApp. |

**Trade-off.** The "clear yes" check understands English only; anything else
just makes the bot ask again.

---

## 11. Knowledge retrieval (RAG) — fastembed + bge-small

**Role.** The FAQ (`knowledge/faq.md`, 20 sections on shipping, returns,
payments, warranties, gift cards, accounts and the cloud-storage service) is
split at each `##` heading, turned into 384-number vectors by
`BAAI/bge-small-en-v1.5` running locally through `fastembed`, and stored in
Postgres. For each question, the 3 closest sections go into the prompt.

**Why it matters.** Policy answers come from your own documents, with sources
shown, and change by editing a Markdown file — no retraining. Search is by
*meaning*: "will my stuff get wiped if I stop paying?" finds the cancellation
section without sharing its words. For follow-ups, the previous question is
searched too, so "how much is it?" still finds the right section.

**Why RAG + this embedding model over the alternatives**

| Alternative | Why not |
|---|---|
| Fine-tuning the LLM on the FAQ | Expensive, slow to update, and answers can't point to a source. |
| OpenAI / hosted embedding APIs | Per-call cost, a second external dependency, and FAQ text leaves our infrastructure. |
| `sentence-transformers` | Same quality class, but pulls in PyTorch (hundreds of MB); `fastembed` uses ONNX on CPU. |
| `bge-base` / larger models | Better ranking, but slower and bigger; not needed for 20 sections. |

**Trade-offs.** English-focused; a small model ranks bluntly (scores clustered
in a narrow 0.5–0.7 band, so the best match is sometimes 2nd or 3rd — still
caught by taking the top 3); changing the model means re-indexing; the whole
FAQ is re-embedded on every startup (trivial now, wasteful for a large
knowledge base). FAQ edits need `docker compose restart app` to be indexed.

---

## 12. Vector search — pgvector

**Role.** A PostgreSQL extension that stores the FAQ vectors and finds the
nearest ones by cosine similarity, using an HNSW index.

**Why it matters.** It keeps search in the same database as everything else.

**Why pgvector over the alternatives**

| Alternative | Why not |
|---|---|
| Pinecone (hosted) | Another paid service, account and network hop; data leaves our infrastructure. |
| Qdrant / Weaviate / Milvus | Excellent at millions of vectors, but another server to run, secure and back up. |
| Chroma | Simple, but a separate store; less proven for production. |
| FAISS | A library, not a database: no persistence, transactions or SQL. |

**Trade-offs.** Dedicated vector databases scale further and offer more search
features; pgvector needs the `pgvector/pgvector` image instead of stock
Postgres. At hundreds or thousands of chunks, it's more than enough.

---

## 13. Database — PostgreSQL

**Role.** Stores everything: customers, product types, products and variants,
orders and order lines, order drafts, cancellation requests, tickets, and the
FAQ vectors.

**Why it matters — Postgres guarantees the hard parts, even if app code has a
bug.**
- **Transactions:** an order, its line items and the stock decrement succeed
  or fail together.
- **Row locks (`SELECT ... FOR UPDATE`):** two customers buying the last item
  can't both succeed; two "yes" messages can't place one quote twice.
- **Savepoints:** if a price changed since the quote, the stock already taken
  is put back before anything is saved.
- **Advisory locks:** two admins can't claim the same SKU at once (§14).
- **Constraints:** foreign keys, unique SKUs and slugs, and CHECK constraints
  (no negative price or stock; status values limited to known ones).
- **JSONB and pgvector** in the same engine (§12, §14).

**Why PostgreSQL over the alternatives**

| Alternative | Why not |
|---|---|
| MySQL / MariaDB | Capable relational database, but weaker JSON indexing and no pgvector — we would need a separate vector store. |
| MongoDB | Flexible documents, but orders need relational integrity (foreign keys, multi-row transactions, row locks) that Postgres gives natively — and Postgres's JSONB covers the flexible parts. |
| SQLite | Great for tests and single users, but limited concurrent writes and no pgvector. |

**Trade-off.** One more service to run than SQLite — handled by Docker (§19).

---

## 14. Flexible catalog data — JSONB

**Role.** Postgres's binary JSON type stores the parts of the data whose shape
varies:

| Column | Holds |
|---|---|
| `product_types.attribute_schema` | Each type's attribute definitions (Books: author, format, pages; Clothing: size, colour, material...), including which attributes **vary per variant** |
| `products.attributes` | A product's values for those attributes |
| `products.variants` | Its sellable versions — each with its own SKU, options (e.g. size M, colour black), optional price, stock and active flag |
| `order_drafts.lines` | The items in a pending quote |

**Why it matters.** Store owners can add a new product type with new
attributes (e.g. Toys with "minimum age") through the API — **no migration,
no code change**. The app validates every product against its type: required
attributes present, no unknown ones, correct kinds, choices from the allowed
list, variant options unique.

**Why JSONB over the alternatives**

| Alternative | Why not |
|---|---|
| A column per attribute | Every new attribute means a migration; most columns would be empty for most products. |
| EAV tables (entity–attribute–value) | Flexible, but queries become many joins and type-checking moves into awkward SQL. |
| A separate `product_variants` table | Real foreign keys and unique constraints, but more joins and more code for a catalog of this size. JSONB was chosen deliberately. |
| MongoDB | See §13 — would sacrifice relational integrity for orders. |

**Trade-offs — what JSONB gives up, and how we compensate.**
- **No unique constraint inside a JSON array**, so variant SKUs can't be made
  unique by the database. The app checks uniqueness across all products and
  variants under per-SKU advisory locks, with a GIN index (`jsonb_path_ops`)
  so the lookup stays fast. A direct database edit could still create a
  duplicate.
- **No foreign key to a variant**, so order lines record the variant's SKU and
  a name snapshot; deactivating a variant is preferred over deleting it.
- **Price filters use the product's base price**, not each variant's.

---

## 15. Data access — SQLAlchemy 2.0 + psycopg2

**Role.** SQLAlchemy models (`app/models/`) define each table once — columns,
types, relationships, constraints — for the API, chat tools, seed script and
migrations. `psycopg2` is the driver that talks to Postgres. A pooled engine
reuses connections across requests.

**Why it matters.** Queries are built from Python expressions, so they're
parameterised (no SQL injection) and readable; transactions, row locks and
savepoints are expressed directly (`with_for_update()`, `begin_nested()`).

**Why SQLAlchemy over the alternatives**

| Alternative | Why not |
|---|---|
| Raw SQL everywhere | Full control, but repetitive and easy to get subtly wrong across dozens of queries. (The pgvector store does use raw SQL, where it's simplest.) |
| Django ORM | Tied to Django (§4). |
| SQLModel | Thin layer over SQLAlchemy + Pydantic; adds a dependency without changing much here. |
| Async ORMs (Tortoise, async SQLAlchemy) | Truly async, but the rest of the stack (embeddings, the model client) is synchronous; the thread pool is simpler. |

**A lesson from development.** `SQLAlchemy>=2.0.30` is unpinned, and a
rebuild installed **2.1**, which changed the default driver for
`postgresql://` URLs to psycopg 3 (not installed) — the app stopped starting.
The fix (`sqlalchemy_url()` in `app/db/session.py`) names the `psycopg2`
driver explicitly. Pinning major versions (e.g. `<2.2`) would prevent similar
surprises.

---

## 16. Schema changes — Alembic

**Role.** Versioned migration scripts in `migrations/versions/`, applied
automatically at startup:

| Migration | Change |
|---|---|
| 0001 | Baseline: customers, orders, tickets |
| 0002 | Products, order line items, admin flag (converted old free-text orders without data loss) |
| 0003 | Product types; existing products moved to a "General" type |
| 0004 | Order drafts (bot quotes) |
| 0005 | Cancellation requests |
| 0006 | Product variants (JSONB) and `variant_sku` on order lines |

**Why it matters.** Every schema change is reviewable, repeatable and applied
the same way on every machine — and data migrations (like converting legacy
orders) are part of the history, not a one-off script someone ran.

**Why Alembic over the alternatives**

| Alternative | Why not |
|---|---|
| `Base.metadata.create_all()` | Creates missing tables but never alters existing ones — useless after the first deploy. |
| Hand-run SQL scripts | No version tracking; easy to skip or repeat one. |

**Trade-off.** Running migrations at startup is convenient for one instance
but can race with several; a production deploy should run them as a separate
step.

---

## 17. Authentication — Argon2 + JWT

**Role.** Passwords are hashed with **Argon2id** (`argon2-cffi`). Logging in
(`POST /auth/login`, OAuth2 password form) returns a signed **JWT**
(`PyJWT`, HS256) used as `Authorization: Bearer ...` — or sent in the first
WebSocket message. An `is_admin` flag gates catalog changes and order/ticket
status updates.

**Why it matters.**
- **The token decides whose data the bot sees** — never an email or order
  number typed into a question.
- **Login doesn't leak which emails exist:** wrong password, unknown email and
  passwordless account all return the same message and take the same time.
- **Invalid tokens are never quietly treated as "signed out"** — the client is
  told to log in again.

**Why these over the alternatives**

| Alternative | Why not |
|---|---|
| bcrypt / PBKDF2 | Still acceptable, but Argon2id is the current recommendation (memory-hard, resists GPU cracking). |
| Server-side sessions | Need a session store; JWT is verified from its signature alone, and works for REST and WebSocket identically. |
| Keycloak / Auth0 | Full identity platforms (SSO, MFA) — more than a single store needs today, and another service or vendor. |

**Trade-offs.** JWTs can't be revoked before they expire (default 60 minutes);
one shared secret signs all tokens (rotating it logs everyone out); no login
rate limiting yet; HTTPS is needed in production.

---

## 18. Testing — pytest, fakes and a real database

**Role.** 200+ automated tests in `tests/`:

| Kind | How | Covers |
|---|---|---|
| Tool-calling agent | A scripted **fake LLM** | Customer scoping, tool offers, argument validation, retry/failover rules, confirmation checks |
| Pure rules | Plain functions | Attribute and variant validation, "clear yes" detection |
| WebSocket protocol | FastAPI `TestClient` | Auth, history, reset, errors, bad frames |
| Database behaviour | **Real Postgres**, each test inside a transaction that's rolled back | Ordering variants, stock, row locks, savepoints, drafts, cancellations, SKU uniqueness |

**Why it matters.** The safety properties — "a customer can't see another's
orders", "the bot can't order without a yes", "a price change orders nothing"
— are pinned by fast, free, repeatable tests rather than by hoping the live
model behaves. The database tests caught a real bug: an order naming a
variant the product didn't have was silently placed without it.

**Why this approach over the alternatives**

| Alternative | Why not |
|---|---|
| Testing against the live model | Slow, costs credits, and gives different results run to run. |
| Mocking the database | Can't test locks, savepoints or JSONB queries — the parts most likely to be wrong. |

**Trade-off.** Database tests need Postgres running; they skip themselves
otherwise. Run everything with
`docker compose run --rm app sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q"`.

---

## 19. Packaging — Docker and Docker Compose

**Role.** `docker compose up -d` starts two containers: the API (built from
the `Dockerfile`, with the embedding model baked in at build time) and
Postgres with pgvector (`pgvector/pgvector:pg16`). The project folder is
mounted and Uvicorn runs with `--reload`, so code edits apply instantly.

**Why it matters.** Anyone can run the whole system with one command and no
local Postgres installation, and get the same versions everyone else has.

**Why Docker Compose over the alternatives**

| Alternative | Why not |
|---|---|
| Installing Python and Postgres locally | Version drift between machines; the pgvector extension must be compiled or installed separately. |
| Kubernetes | The right tool for many servers; far too much for one API and one database. |

**Trade-offs.** The setup is development-oriented — mounted code, `--reload`,
a hardcoded database password, host port 8020 (8000 was taken on the dev
machine) — and must be hardened for production. Changes to `.env` need
`docker compose up -d --force-recreate app`; a plain restart doesn't re-read it.

---

## 20. Configuration — environment variables

**Role.** Settings come from the environment (a `.env` file via
`python-dotenv`, or Compose): `HF_TOKEN`, `HF_FALLBACK_TOKENS`,
`DATABASE_URL`, `JWT_SECRET`, `JWT_EXPIRE_MINUTES`, `LLM_MODEL`,
`LLM_PROVIDER`. `app/config.py` reads them once and **fails at startup with a
clear message** if a required one is missing or `JWT_SECRET` is shorter than
32 characters.

**Why it matters.** Secrets stay out of the code and the repository
(`.env` is git-ignored; `.env.example` documents every setting), and the same
image runs anywhere with different settings.

**Why over the alternatives.** Config files in the repo risk committing
secrets; secret managers (Vault, AWS Secrets Manager) are the right move in
production and plug into the same environment variables.

---

## 21. Planned — WhatsApp

**Plan.** Let customers chat over WhatsApp using **Meta's WhatsApp Cloud API**:
a webhook endpoint receives messages, the existing chat service answers, and
replies go back through Meta's send API.

**Why the Cloud API.** It's Meta's own hosted API (no servers of ours to run
for WhatsApp itself); **Twilio's** WhatsApp offering is easier to prototype
with (a sandbox) but adds a middleman and its own fees on top of Meta's.

**What the current design already provides.** Orders and cancellations
confirm by text ("yes"), not only by button (§10); quotes and requests are
stored in Postgres, not in a browser. **Still needed:** linking a WhatsApp
number to a customer account safely (with verification), storing conversation
history in Postgres, and signature checks on the webhook. Since 1 October
2026, Meta charges for service (reply) messages too.

---

## 22. The biggest trade-offs, and when to revisit them

| Trade-off today | Revisit when... | Likely move |
|---|---|---|
| The model is billed per token, and free credit runs out after a few conversations | Volume grows, or customer data must stay in-house | Paid HF plan, or self-host the open model with vLLM |
| Conversation history is in server memory | Adding WhatsApp, or running several API instances | Store history in Postgres |
| Variants in JSONB: uniqueness and references enforced by the app, not the database | Catalog grows large, or other systems write to the database directly | A `product_variants` table |
| Small English embedding model | The FAQ grows large or multilingual | `bge-base` / multilingual model, or a reranker |
| Migrations run at startup | More than one API instance | Run migrations as a deploy step |
| JWTs can't be revoked; no login rate limit | Before real customers | Short-lived tokens + refresh, rate limiting, HTTPS |
| One-file web page | A full storefront is needed | A frontend framework |
| "Clear yes" check is English-only | Non-English customers | Language-aware confirmation, or buttons on every channel |
