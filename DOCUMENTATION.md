# Support Bot — Project Documentation

A record of everything built so far: what the system does, how it is put together, the order in which it was built, the problems hit along the way, and what is still open.

---

## 1. Overview

The Support Bot is a customer-support assistant for a fictional product, **Acme Cloud Storage**, exposed as an authenticated HTTP API. It has two halves:

- **A store back-end** — REST resources for **products, orders (with line items) and tickets**, stored in Postgres, with customer/admin access control.
- **An agentic chatbot** — for each question, the model decides for itself what data it needs and requests it through **tool calling**: read-only tools that search the live product catalog or look up the signed-in customer's own orders and tickets. FAQ text (policies, how-tos) is still retrieved up front by semantic search, since it's cheap, local, and doesn't need a tool round-trip.

The model never touches the database and never writes SQL. Every tool call runs a fixed, parameterised query scoped to the *authenticated* customer, never to an id the model supplies. Signed-out callers can ask general and product questions; anything needing account data requires a login.

| Layer | Technology |
|---|---|
| API | FastAPI + Uvicorn; **class-based views** for the store resources |
| ORM / migrations | SQLAlchemy 2.0 models, Alembic migrations |
| Authentication | Argon2 password hashing, JWT (HS256) bearer tokens, OAuth2 password flow, admin role |
| LLM | `Qwen/Qwen2.5-72B-Instruct` (pinned to the `deepinfra` provider) via Hugging Face Inference Providers — **billed**, not free |
| Agent behaviour | Tool calling (`app/services/tools.py`), 1–4 model round-trips per question |
| Embeddings | `BAAI/bge-small-en-v1.5` via `fastembed` (local, ONNX, 384 dims) |
| Database | PostgreSQL 16 with the `pgvector` extension |
| Testing | pytest, a scripted fake LLM (no network/credits needed) |
| Packaging | Docker + Docker Compose |

---

## 2. Build History (chronological)

| # | Task | Outcome |
|---|---|---|
| 1 | **Project structure cleanup** | `chat.py` was misplaced in a stray `chunks/` folder; moved to the root. Created a `.venv`. |
| 2 | **Switch from Anthropic to a free open model** | Replaced the Anthropic SDK with `huggingface_hub`'s `InferenceClient`. Auth moved to `HF_TOKEN`. (Later corrected: see #12 — no provider on the router is actually free.) |
| 3 | **Secrets handling** | `python-dotenv`, `.env` / `.env.example`, `.gitignore`. |
| 4 | **Fix HF 403** | Token lacked the *"Make calls to Inference Providers"* scope. |
| 5 | **Fix HF 400 (`model_not_supported`)** | Switched from Qwen2.5-7B to `microsoft/phi-4` after checking the live router model list. |
| 6 | **Database integration** | Postgres lookups of customers/orders/tickets and identifier extraction. |
| 7 | **Dockerization** | `Dockerfile` + `docker-compose.yml`; resolved host-port conflicts. |
| 8 | **FastAPI restructure + API endpoint** | Replaced the terminal loop with `POST /chat`; `app/api`, `app/schemas`, `app/services` layout. |
| 9 | **pgvector semantic retrieval** | Replaced TF-IDF with embedding search in Postgres. |
| 10 | **Authentication** | Login/registration, JWT tokens, argon2, per-customer data scoping. |
| 11 | **Models, class-based views, and DB-backed answers** | SQLAlchemy models + Alembic migrations; products / orders / tickets REST resources as class-based views with role-based access; chat answers now come from the live `products` catalog and the customer's real orders. Replaced `schema.sql` and the raw-SQL data layer. **Breaking schema change** — see §6. |
| 12 | **Tool calling** | Replaced the fixed "always fetch the catalog + customer data" design with a model-driven tool-calling loop. Required switching models (`phi-4` cannot do tool calling — see §11) to `Qwen2.5-72B-Instruct`. Corrected a standing inaccuracy in this project's own docs: **no Hugging Face router provider is actually free**; the LLM is billed per token. Added the first automated test suite (pytest, against a scripted fake model). **Breaking API change** — see §7.3. |

---

## 3. Architecture

```
        POST /chat  { question }   [+ Authorization: Bearer <jwt>]
                              │
                              ▼
                   app/api/routes/chat.py
        (api/deps.py: token → customer, or None; bad token → 401)
                              │
                              ▼
                app/services/chat_service.answer_question()
                              │
        signed out + account-type question?  ──yes──▶  "please log in"
                              │ no                     (no model call)
                              ▼
              vector_knowledge_base.retrieve()   (FAQ chunks, always -- cheap, local)
                              │
                              ▼
              ┌─────────────────────────────────────────────┐
              │  TOOL-CALLING LOOP (≤ MAX_TOOL_STEPS rounds) │
              │  tools offered = tools_for(customer)         │
              │                                              │
              │   llm.complete(messages, tools) ──tool_calls?│
              │       ▲                    │ yes             │
              │       │                    ▼                 │
              │       │        run_tool(name, args, ctx)      │
              │       │        ctx.customer = the AUTHENTICATED│
              │       │        customer -- never an id from   │
              │       │        the model's own arguments       │
              │       │                    │                  │
              │       └── append result, loop back ───────────┘
              │                    │ no tool_calls
              │                    ▼
              │              done: answer text
              └─────────────────────────────────────────────┘
                              ▼
       { answer, sources, needs_authentication, tools_used }
```

```
   REST resources (class-based views)          Data
   ProductView  ─┐
   OrderView    ─┼─ Depends(get_session) ──▶  SQLAlchemy Session ──▶ Postgres
   TicketView   ─┘   Depends(auth/admin)      (pooled engine)        products, orders,
                     services/orders.py                              order_items, tickets,
                     (stock, price, status)                          customers
```

**Design principles**

- **Retrieval is separate from generation.** FAQ text is fetched once, up front; the model still only sees what was explicitly given to it or what it explicitly requested.
- **The model decides what it needs; the server decides what it's allowed to have.** Tool calling lets the model choose *which* tool and *what search/lookup arguments* (a product query, an order number). It never chooses *whose* data — every account tool reads the customer from the verified login token via `ToolContext`, not from the model's arguments. See §5.6 and the module docstring of `app/services/tools.py`.
- **The model never touches the database and never writes SQL.** Only the fixed, read-only tools in `tools.py` exist for it to call. (Letting an LLM generate SQL would hand anyone who can chat a path to the whole database.)
- **Authorization is enforced by what a tool is allowed to fetch, not by what the model is told.** A signed-out caller isn't even offered the account tools; another customer's order returns the same "not found" as a nonexistent one. The system prompt also tells the model to decline such questions, but that's not the safeguard — the data being unreachable is.
- **The server owns the money.** Clients send product ids and quantities only; prices, totals, status and ownership are set server-side.

---

## 4. Project Structure

```
support-bot/
├── app/
│   ├── main.py                    # FastAPI app; lifespan: migrate → index FAQ → build clients
│   ├── config.py                  # env-driven settings (fail-fast), incl. LLM_MODEL/LLM_PROVIDER
│   ├── db/
│   │   ├── base.py                # SQLAlchemy DeclarativeBase
│   │   ├── session.py             # pooled engine + session factory
│   │   └── migrate.py             # runs `alembic upgrade head` in-process
│   ├── models/                    # ORM models
│   │   ├── customer.py  product.py  order.py (Order, OrderItem)  ticket.py
│   │   └── enums.py               # status / billing-period values shared with schemas
│   ├── api/
│   │   ├── deps.py                # token → customer; require_admin; get_session
│   │   ├── routes/                # function-based: auth.py, chat.py, health.py
│   │   └── views/                 # CLASS-BASED views
│   │       ├── base.py            # APIView + @route
│   │       └── products.py  orders.py  tickets.py
│   ├── schemas/                   # pydantic: auth, chat, product, order, ticket
│   └── services/
│       ├── auth.py                # argon2 + JWT
│       ├── orders.py              # order rules (price snapshot, stock, transitions)
│       ├── errors.py              # DomainError → HTTP status
│       ├── database.py            # ORM-backed helpers for tools/login (return dicts)
│       ├── llm.py                 # HF chat client wrapper: model/provider, retry, 503 on failure
│       ├── tools.py               # TOOL CALLING: schemas + handlers the model may invoke
│       ├── customer_lookup.py     # signed-out gate only (spots account-type questions)
│       ├── chat_service.py        # retrieve FAQ → tool-calling loop → answer
│       ├── vector_knowledge_base.py  embeddings.py  document_loader.py
├── migrations/                    # Alembic: env.py, versions/0001, 0002
├── alembic.ini
├── knowledge/faq.md               # FAQ source (8 sections)
├── seed_data.py                   # migrate + demo admin/customers/products/orders/tickets
├── tests/test_tool_calling.py     # pytest: the loop + tools, against a scripted fake LLM
├── Dockerfile  docker-compose.yml  requirements.txt  requirements-dev.txt
├── .env.example  .gitignore  .dockerignore
├── README.md  DOCUMENTATION.md  TECH_STACK.md
```

---

## 5. Components in Detail

### 5.1 `app/main.py`
The `lifespan` hook, once at startup: (1) `run_migrations()`; (2) creates the pooled engine + session factory; (3) indexes `knowledge/` into pgvector; (4) builds `LLMClient`. Everything is stored on `app.state` (`session_factory`, `db`, `kb`, `llm`) and the engine is disposed on shutdown. Registers the function-based routers, the three class-based view routers, and an exception handler mapping `DomainError` → JSON `{"detail": …}` with the right status (and `Retry-After` for 503).

### 5.2 `app/config.py`
`Settings` from a cached `get_settings()`; **fails fast** if `HF_TOKEN`, `DATABASE_URL` or `JWT_SECRET` is missing, or if `JWT_SECRET` is shorter than 32 characters. Also holds `llm_model`/`llm_provider`, read from `LLM_MODEL`/`LLM_PROVIDER` env vars, defaulting to `Qwen/Qwen2.5-72B-Instruct` on `deepinfra`.

### 5.3 Models (`app/models/`)
SQLAlchemy 2.0 typed models, with CHECK constraints mirrored from the migrations so the database enforces the rules even if application code is bypassed:

| Model | Notes |
|---|---|
| `Customer` | `email` unique, `plan`, `password_hash` (nullable), `is_admin`, `created_at` |
| `Product` | `sku` unique; `price ≥ 0`; `stock_quantity` NULL = unlimited, else `≥ 0`; `billing_period ∈ {one_time, monthly, yearly}`; `is_active` |
| `Order` | `customer_id` FK; `status ∈ {pending, processing, completed, cancelled}`; `total_amount`; `items` (selectin-loaded) |
| `OrderItem` | `order_id` FK (cascade delete), `product_id` FK, **`product_name` and `unit_price` copied at purchase time**, `quantity > 0` |
| `Ticket` | `customer_id` FK; `subject`, `description`; `status ∈ {open, in_progress, resolved, closed}` |

Snapshotting name and price on `OrderItem` means a later price change or rename never rewrites order history.

### 5.4 Migrations (`migrations/`)
Alembic with `env.py` reading `DATABASE_URL` (never stored in `alembic.ini`); the pgvector `documents` table is excluded from autogenerate (`include_name`).

- **`0001_baseline`** — the pre-Alembic tables (`customers`, `orders`, `tickets`), every statement `IF NOT EXISTS`, so it is a no-op on an existing database and creates them on a new one.
- **`0002_products_orders_tickets`** — adds `customers.is_admin/created_at`, `products`, `order_items`; adds `orders.total_amount/updated_at`; adds `tickets.description/updated_at`; adds CHECK constraints and indexes. **Converts legacy data**: each old order (`item`, `amount`) becomes one `order_items` row pointing at an *inactive* "legacy" product, then `orders.item/amount` are dropped. Irreversible (downgrade raises).

The app applies pending migrations at startup (convenient for development; with several instances, run `alembic upgrade head` as a single deploy step instead so they don't race).

### 5.5 Class-based views (`app/api/views/`)
FastAPI has no built-in class-based views, so `base.py` provides a small dependency-free `APIView` + `@route(method, path, **kwargs)`:

```python
class TicketView(APIView):
    prefix = "/tickets"
    tags = ["tickets"]

    @route("POST", "", response_model=TicketOut, status_code=201)
    def create_ticket(self, payload: TicketCreate,
                      session: Session = Depends(get_session),
                      customer: dict = Depends(get_current_customer)): ...

app.include_router(TicketView.as_router())
```
Methods are ordinary FastAPI handlers (`self` isn't part of the request signature); dependencies arrive as parameters, so a single instance holds no per-request state. Routes register in definition order and a subclass can override an endpoint by redefining the method.

| View | Behaviour |
|---|---|
| `ProductView` | Public reads of **active** products (search, pagination); inactive products look nonexistent to non-admins; admin create/PATCH/DELETE; duplicate SKU → 409; deleting a product that appears on an order → 409 (deactivate instead) |
| `OrderView` | Customers create and see only their own orders; admins see all; cancel; admin status changes |
| `TicketView` | Customers open/see/close their own tickets; admins see all and set status |

Inbound schemas use `extra="forbid"`, so unknown fields (`price`, `status`, `customer_id`, `is_admin`, …) are a 422 instead of being silently ignored — a defence against mass-assignment. The same principle is reused for tool arguments (§5.6).

### 5.6 Tool calling (`app/services/tools.py`)
This is the core of the current design. `run_tool(tools, name, raw_arguments, ctx)` executes one model-requested tool call and never raises — every failure mode becomes a JSON error result the model reads and can react to.

**Registered tools:**

| Tool | Login required | Wraps |
|---|---|---|
| `search_products` | No | `Database.search_products` — keyword + max-price + in-stock filter over active products |
| `get_order` | Yes | `Database.get_order_for_customer` — one order by number, scoped to the caller |
| `list_my_orders` | Yes | `Database.get_orders_for_customer` — optional status filter |
| `list_my_tickets` | Yes | `Database.get_tickets_for_customer` — optional status filter |

**The security model, in order of what actually stops abuse:**
1. **No argument schema contains a customer identifier.** `GetOrderArgs` has only `order_id`; there is no field the model could set to another customer's id even if it tried.
2. **`ToolContext.customer` comes from the verified JWT**, set once per request in `chat_service.answer_question`, not from anything in the conversation.
3. **`tools_for(customer)` filters the offered tools** before the model ever sees them — a signed-out caller's tool list contains only `search_products`. `run_tool` re-checks `tool.requires_login` too, so even a tool named by a signed-out model (a possible model error, not a client attack, since the client never controls what tools are offered) is refused.
4. **Ambiguity is intentional:** `get_order` returns the same `"No order with ID N exists on this account"` whether the order belongs to someone else or doesn't exist, so order ids can't be probed for existence.

**Robustness details:**
- **Argument validation** via pydantic (`SearchProductsArgs`, `GetOrderArgs`, etc.) — a non-integer `order_id` becomes an error result, not a crash, and `_parse_arguments` copes with a provider returning `""`, `null`, a dict, or a malformed string for `arguments`.
- **`_clean_schema`** strips pydantic's `title`s and collapses `anyOf: [X, null]` (an optional field) down to plain `X` — smaller, simpler schemas are followed more reliably by a 72B-class model than pydantic's raw, verbose output.
- **Search term cleaning (`search_terms`)** lower-cases, drops filler words ("the", "under", "dollars", …) and short/plural noise, and caps at 5 terms, so a natural-language query like *"do you have any plan under 10 dollars a month"* becomes the single keyword `plan` rather than matching everything or nothing.
- **Result size cap** (`MAX_RESULT_CHARS`) — an oversized result becomes an error asking for narrower results, rather than blowing up the context.
- **Tool results are explicitly labelled as data, not instructions**, in the system prompt — a ticket description is customer-written text that could contain a prompt injection attempt.

### 5.7 The tool-calling loop (`chat_service.answer_question`)
1. **Login gate** — unchanged from before tool calling: if signed out and the question needs account access (`customer_lookup.needs_account_access`), return the login prompt immediately. No retrieval, no model call.
2. **FAQ retrieval** — top-`k` chunks from pgvector, folded into the first user message. Kept outside the tool loop because it's free (local embeddings) and policy/how-to text doesn't belong behind a tool.
3. **The loop** (`MAX_TOOL_STEPS = 4` rounds, `MAX_CALLS_PER_STEP = 3` calls honoured per round):
   - Call `llm.complete(messages, tools=schemas)`.
   - No `tool_calls` in the reply → that's the answer; return it.
   - Otherwise, append the assistant's tool-call message, run each call (skipping any beyond the per-step cap, and short-circuiting an **exact repeat** of an already-made call so a model that loops on the same call doesn't re-run it or spin forever), append each result as a `role: "tool"` message, and loop.
   - If the cap is reached without a plain answer, one final call is made with the tools removed, forcing the model to answer from what it already has (`FINAL_ANSWER_NUDGE`).
4. Returns a `ChatResult` with `answer`, `sources` (FAQ files only), `needs_authentication`, and `tools_used` (names of tools actually executed, for transparency in the API response).

### 5.8 `app/services/llm.py`
Thin wrapper isolating the provider and its failure modes: retries **once** on a transient 5xx or network error, does **not** retry a 429 (retrying a rate limit only worsens it), and turns any provider failure into a clean **503 + `Retry-After`** instead of an unhandled 500. `temperature=0.2` — low, since tool-argument choices and refusals benefit from consistency more than creative variation.

### 5.9 Authentication (`app/services/auth.py`, `app/api/deps.py`, `routes/auth.py`)
- **argon2id** password hashing; `verify_password` does equivalent work for unknown users so timing doesn't reveal which emails exist.
- **JWT (HS256)** with `sub`/`iat`/`exp`; decoding pins the algorithm and requires `exp` and `sub` (rejecting `alg: none` and non-expiring tokens).
- `get_optional_customer` — no token → anonymous; an *invalid/expired* token → 401 (never silently downgraded). `get_current_customer` requires a login. **`require_admin`** → 403 for non-admins. `get_session` yields one pooled ORM session per request.
- `POST /auth/register` never sets `is_admin` (the schema forbids it) and uses `ON CONFLICT DO NOTHING`, so it can't claim an existing account.

### 5.10 `app/services/orders.py`
- **`create_order`** — merges duplicate lines; caps quantity per product at 100; **locks the product rows** (`SELECT … FOR UPDATE`, in id order to avoid deadlocks); rejects unknown *and* inactive products with the same message; checks and decrements stock; snapshots name/price; computes the total from database prices. Never commits — the view commits once, so any failure (e.g. the last line out of stock) rolls back everything, including stock already decremented.
- **Status transitions** — admin: `pending → processing | cancelled`, `processing → completed | cancelled`; `completed` and `cancelled` are final. A customer may only cancel their own **pending** order.
- **`get_order_for_actor`** — returns the order only to its owner or an admin, otherwise `404` identical to a nonexistent id.

### 5.11 `app/services/database.py`
ORM-backed helpers returning plain dicts, used by both the tools and the auth routes: `get_customer_by_id` (never selects `password_hash`), `get_customer_auth_record`, `create_customer` (`ON CONFLICT DO NOTHING`), `get_order_for_customer`, `get_orders_for_customer`, `get_tickets_for_customer` (all keyed by `customer_id`, filterable by status, capped rows), `search_products` (keyword/price/stock filter over active products). Uses a **pooled** engine.

### 5.12 Retrieval stack
`document_loader.py` (chunk `.md` by `##`, `.txt` by paragraph), `embeddings.py` (local `bge-small-en-v1.5`), `vector_knowledge_base.py` (self-provisions the `vector` extension, `documents` table and HNSW cosine index; wipes and re-indexes `knowledge/` at startup; `retrieve()` returns top-k by cosine similarity). It still uses its own psycopg2 connections, separate from the pooled ORM engine.

### 5.13 `app/services/customer_lookup.py` (trimmed)
Now holds *only* the signed-out gate: `extract_email`, `extract_order_id`, `looks_personal`, `needs_account_access`. Its former job of building the signed-in customer's context (`build_customer_context`) is gone — that's now what the tools in `tools.py` do, scoped by the authenticated customer rather than by parsing the question.

---

## 6. Data Model

**Managed by Alembic** (`app/models/`)

| Table | Columns |
|---|---|
| `customers` | `id`, `name`, `email` (unique), `plan`, `password_hash` (nullable), `is_admin`, `created_at` |
| `products` | `id`, `sku` (unique), `name`, `description`, `price NUMERIC(10,2)`, `billing_period`, `stock_quantity` (nullable), `is_active`, `created_at`, `updated_at` |
| `orders` | `id`, `customer_id → customers`, `status`, `total_amount`, `created_at`, `updated_at` |
| `order_items` | `id`, `order_id → orders (cascade)`, `product_id → products`, `product_name`, `unit_price`, `quantity` |
| `tickets` | `id`, `customer_id → customers`, `subject`, `description`, `status`, `created_at`, `updated_at` |

**Managed by the app itself:** `documents` (`id`, `source`, `content`, `embedding VECTOR(384)`, HNSW index) — rebuilt from `knowledge/` on every start.

**Breaking change from earlier versions:** `orders.item` / `orders.amount` no longer exist. Existing orders were migrated into `order_items` (see §5.4). `schema.sql` was removed — Alembic is the source of truth.

**Demo data** (`seed_data.py`, idempotent — only adds what's missing):

| Accounts | Products |
|---|---|
| `admin@example.com` (admin), `ada@example.com` (pro), `grace@example.com` (plus) — **random passwords, printed once** | Free / Plus / Pro plan (monthly), Pro plan – annual, Extra 500GB add-on, **Acme Backup Drive 1TB** ($89, 10 in stock — exercises the stock logic) |

Orders (Ada: Pro plan – annual, completed; Grace: 500GB add-on, processing) and tickets are inserted only into an empty database. The seed **never promotes a pre-existing account to admin** (someone could have registered `admin@example.com` first); it prints a warning instead.

---

## 7. API Reference

### 7.1 Authentication
- `POST /auth/register` — JSON `{name, email, password}` (name 1–100; email `x@y.z`, trimmed + lower-cased; password 8–128). `201` profile, `409` if the email exists, `422` on bad input or unknown fields.
- `POST /auth/login` — form fields `username` (email) and `password` → `{access_token, token_type, expires_in}`. One identical `401` for every failure.
- `GET /auth/me` — the caller's profile `{id, name, email, plan, is_admin}`.

Send `Authorization: Bearer <token>`; tokens last `JWT_EXPIRE_MINUTES` (default 60), no refresh endpoint.

### 7.2 Products, orders, tickets

| Endpoint | Signed out | Customer | Admin |
|---|---|---|---|
| `GET /products`, `GET /products/{id}` | active only | active only | + inactive (`?include_inactive=true`) |
| `POST /products` · `PATCH /products/{id}` · `DELETE /products/{id}` | 401 | 403 | yes |
| `POST /orders` | 401 | for yourself | yes (as themselves) |
| `GET /orders` · `GET /orders/{id}` | 401 | own only | all (`?customer_id=`, `?status=`) |
| `POST /orders/{id}/cancel` | 401 | own, while `pending` | any cancellable |
| `PATCH /orders/{id}/status` | 401 | 403 | yes |
| `POST /tickets` · `GET /tickets` · `GET /tickets/{id}` | 401 | own only | all |
| `POST /tickets/{id}/close` | 401 | own | yes |
| `PATCH /tickets/{id}/status` | 401 | 403 | yes |

List endpoints take `limit` (1–100, default 20) and `offset`. Other people's orders/tickets → `404`. Money is a JSON string (`"89.00"`) to avoid float error. Product responses include a computed `in_stock`; **exact stock levels are public** (see limitation 12).

Place an order:
```json
POST /orders
{ "items": [ {"product_id": 6, "quantity": 2}, {"product_id": 2, "quantity": 1} ] }
```
Errors: `422` (bad quantity, unknown/inactive product, unknown field), `409` (not enough stock; invalid status transition; duplicate SKU; product is on orders).

### 7.3 `POST /chat` (tool calling)
`Authorization` optional. Request `{ "question": "…" }` (1–2000 chars). Response:
```json
{ "answer": "...", "sources": ["faq.md"], "needs_authentication": false, "tools_used": ["get_order"] }
```

**Breaking change:** the pre-tool-calling API's `session_id` (request) and `needs_clarification` (response) fields are gone. There is no cross-request session memory any more — each `/chat` call is independent regardless of login state (this was already true; tool calling didn't change it, see limitation "no conversation memory"). `needs_authentication` replaces `needs_clarification`'s role: the bot never needs to ask "what's your email?" since identity now always comes from the token, not from something typed into the question.

| Caller | Question | Result |
|---|---|---|
| Signed out | general / FAQ | Answered from FAQ |
| Signed out | product / pricing | May call `search_products` (the only tool it's offered) |
| Signed out | mentions "my order", an order number, or an email | `needs_authentication: true`, no model call |
| Signed in | anything | FAQ + whichever tools the model chooses, scoped to **that customer's own** data |
| Any | provider busy/failing | `503` + `Retry-After` |
| Bad/expired token | anything | `401` |

### 7.4 Other
`GET /health` → `{"status":"ok"}`. Swagger UI at `/docs` (use **Authorize** once to act as a customer or admin), OpenAPI at `/openapi.json`.

---

## 8. Configuration

| Variable | Where set | Purpose |
|---|---|---|
| `HF_TOKEN` | `.env` | Hugging Face token with the **"Make calls to Inference Providers"** permission. **Billed** — see §9's cost note |
| `LLM_MODEL` / `LLM_PROVIDER` | `.env` (optional) | Default `Qwen/Qwen2.5-72B-Instruct` / `deepinfra`. Must be a pair where the provider supports tool calling — verified empirically (§11) since `phi-4`, the original model, does not |
| `JWT_SECRET` | `.env` | Signs login tokens. **Required, ≥32 chars.** Changing it logs everyone out |
| `JWT_EXPIRE_MINUTES` | `.env` (optional) | Token lifetime, default 60 |
| `DATABASE_URL` | `docker-compose.yml` (Docker) or `.env` | Postgres URL; in Docker it points at the `db` service and overrides `.env`. Also read by Alembic |

`.env` is gitignored and dockerignored. The compose DB credentials are dev-only.

---

## 9. Running the Project

```bash
cp .env.example .env                              # set HF_TOKEN and JWT_SECRET
docker compose up -d                              # db + app; tables are created by migrations at startup
docker compose run --rm app python seed_data.py   # demo data; COPY the printed passwords
```
API at `http://localhost:8020` (8000 was already taken on this machine). Uvicorn runs with `--reload` on a bind-mounted project, so code edits apply immediately; rebuild only when `requirements.txt` changes.

**Cost note:** the LLM is billed against the HF account's credits — no provider on the router is free (verified against the live listing, §11). A `402 Payment Required` from `/chat` means the account is out of credit; the tool-calling loop uses more tokens per question than the earlier single-call design (§10).

**Tests** (a scripted fake model — no network, no Postgres, no credits):
```bash
docker compose run --rm app sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q"
```

**Migrations:** `docker compose run --rm app alembic upgrade head` · `alembic current` · `alembic check` (models vs database) · `alembic revision --autogenerate -m "msg"` (always review the generated file).

**Upgrading an existing database:** just start the app — the migrations convert existing orders into line items. Run the seed to add an admin, the catalog, and passwords for any customer that lacks one.

**Inspect the DB:** `docker compose exec db psql -U supportbot -d supportbot`

---

## 10. Testing Performed

### 10.1 Automated (pytest, `tests/test_tool_calling.py`, 26 tests, committed to the repo)

Run against a scripted `FakeLLM` (a queue of canned replies) and a `FakeDB` — no network, no Postgres, no HF credits, so these run in well under a second and are part of the repo rather than an ad-hoc script.

- **The loop:** signed-out account questions are gated before any model call; a plain answer with no tool calls passes through; a tool call is executed and its result fed back correctly; **the model-supplied `customer_id` is ignored** and the authenticated id is used instead; someone else's order and a nonexistent order produce an identical message; signed-out callers are offered only `search_products`; a signed-out model naming an account tool anyway is refused and the tool never runs; invalid arguments (a non-integer `order_id`) become an error result, not a crash; numeric-string/missing/dict argument shapes are all accepted; a failing tool handler is reported without leaking its exception text; **an identical repeated tool call is executed only once**; calls beyond the per-step cap are skipped but every call still gets a response message; the step cap forces a final tools-off answer; an empty model reply gets a safe fallback; a natural-language product query is cleaned into the right keywords and a `Decimal` price.
- **The tools directly:** search-term cleaning (stopwords, plurals, non-alphanumeric input); no tool schema exposes a customer identifier; schemas are flattened (no `title`, no `anyOf`/`null`) for a small model to follow reliably; an oversized result is rejected rather than sent.
- **The LLM wrapper:** 429 → 503 with no retry; a transient 500 is retried once and succeeds; two 500s become 503; non-5xx client errors (402, 405) are not retried; a network error is retried; the `tools` kwarg is only sent when tools are provided, and `temperature`/`tool_choice` are set correctly.

### 10.2 Live, manual (against the real Docker stack and the real LLM)

| Scenario | Result |
|---|---|
| Anonymous FAQ question ("under $10/month") | Answered from the FAQ text directly — **did not call `search_products`**, even though the tool was offered and relevant (see limitation, §12) |
| Anonymous question the FAQ can't answer ("backup drive, how many in stock") | Correctly called `search_products`; correct price/stock in the answer |
| Signed in, own order status | Correctly called `get_order`, correct data |
| Signed in, "what have I ordered" (no order number given) | Correctly called `list_my_orders` |
| Signed in, asking about another customer's order number | `get_order` correctly returned "not found"; nothing leaked |
| Signed in, open tickets | Correctly called `list_my_tickets`, including a ticket created via the REST API |
| Signed out, a ticket question with no trigger phrase ("do I have any open tickets?") | The regex gate missed it (no "my ticket" match) and it reached the model — **but** the model was offered no ticket tool (signed out), so it correctly declined with no data exposure. Confirms the gate is a UX nicety, not the security boundary. |

### 10.3 Model comparison for tool-calling reliability (this is why the model changed)

Tested by sending each candidate the same four questions with the real tool schemas attached, via `docker compose exec app python -c ...` against the live HF router — not assumed from documentation.

| Model (provider) | Result |
|---|---|
| `microsoft/phi-4` (all providers) | **Cannot do tool calling at all.** Its `deepinfra` listing reports `supports_tools: false`; a request with a `tools` list failed. This is the model used everywhere else in this project before now. |
| `Qwen/Qwen2.5-72B-Instruct` (`deepinfra`/`novita`) | Correctly chose `get_order` for an order-status question, correctly chose no tool for a password-reset question, correctly chose `list_my_orders` for "what did I buy", correctly chose `search_products` with `max_price: 10` for a price-constrained question. Adopted as the new default. |
| `meta-llama/Llama-3.1-8B-Instruct` (`deepinfra`) | Unreliable: invented a tool call with a fabricated `order_id: 12345` for a question that needed no tool at all, and re-called the same tool a second time instead of answering after receiving its result. Rejected. |
| `openai/gpt-oss-20b` | Untestable — the HF account returned `402 Payment Required` for every call to it during testing. |

**Also discovered while testing:** the router's own model listing (`https://router.huggingface.co/v1/models`) shows **zero** providers with `is_free: true`, for any model, at the time of testing. Every provider tested (`featherless-ai`, `deepinfra`, `novita`) is paid. This directly contradicts this project's own earlier documentation, which called the setup "free tier" throughout — that was an unverified assumption, now corrected everywhere it appeared.

### 10.4 Regression (from before tool calling)

Re-confirmed still working after the rewrite: 97 API-level checks (products/orders/tickets CRUD, ownership, stock concurrency), the auth flows (registration normalization/uniqueness, login timing-safety, token rejection cases), and the Alembic migration path on both a fresh and an upgraded database. Not re-run as part of this change (no schema or auth code changed), but nothing in this change touches those paths.

---

## 11. Issues Encountered and How They Were Resolved

| Issue | Root cause | Resolution |
|---|---|---|
| `pip install` blocked | Debian/Ubuntu managed Python | Project `.venv` |
| HF `403` / `400 model_not_supported` | Missing token scope; model not hosted by a provider | Recreated the token; switched to `phi-4` |
| Couldn't set up local Postgres | No sudo password | Postgres in Docker |
| Ports 5433 / 8000 in use | Other projects' containers | DB port unpublished; API on 8020 |
| "vector type not found" at startup | `documents` table created only by the seed | `VectorKnowledgeBase` self-provisions |
| Email with spaces rejected at registration | Pattern check ran before the trim/lower-case validator | Normalizer made `mode="before"` |
| Regex backtracking risk | Overlapping classes / adjacent `\s*` on anonymous input | Bounded quantifiers; question capped at 2000 chars |
| `alembic check` reported drift | `orders.status` is `TEXT` in the legacy table but was modelled as `String(16)` | Model changed to `Text` to match |
| Alembic warned "Did not recognize type 'vector'" | Reflecting the pgvector `documents` table | Filter it out *before* reflection with `include_name` (not `include_object`) |
| Two `/chat` calls returned a bare 500 | The HF tier returned a 429 and a dropped response; the API let the exception escape | Retry once on transient errors, no retry on 429, clean 503 + `Retry-After` |
| Test failures on a fresh DB | My own tests assumed the legacy inactive products from the upgraded DB; one even deleted `FREE-PLAN` | Tests now create their own fixtures; DB reset and rerun (97/97) |
| Product search for "backup" matched all plans | Search intentionally covers descriptions, which mention "daily backups" | Not a bug — test expectation corrected |
| **`phi-4` rejected requests with a `tools` list (405/no structured tool_calls)** | Its providers don't support tool calling (`supports_tools: false`/unset) | Tested three models empirically against the router rather than assuming; switched default to `Qwen2.5-72B-Instruct` pinned to `deepinfra` |
| **"Free tier" was wrong throughout this project's own docs** | Never actually verified against the router's pricing data; `huggingface_hub`'s docs use "Inference Providers" branding that reads as free | Queried `https://router.huggingface.co/v1/models` directly: zero providers report `is_free: true` for any model. Corrected in `.env.example`, README, TECH_STACK.md and here |
| **`db` container had stopped** (unrelated to this change) mid-testing, producing a `500` with a DNS-resolution traceback | The container had exited roughly an hour earlier for an unrelated reason | `docker compose up -d db`; not a code bug, flagged to the user rather than silently worked around |
| **A live test got a `503` under real load** | The account hit a `429` from the provider after a burst of test traffic | Correct behaviour, not a bug — `llm.py` is designed to turn a 429 into exactly this; confirmed by retrying after a pause |
| Test expected `search_terms("plans under 10 dollars a month")` to keep `"10"` | Numbers are deliberately dropped from keyword search (they belong in `max_price`, and as a bare keyword `"10"` would match noise like "100GB") | Test expectation corrected, not the code |

---

## 12. Known Limitations and Open Items

**Security**

1. **No brute-force protection** on `/auth/login` and `/auth/register` (no rate limiting or lockout); registration is open to anyone.
2. **Plain HTTP** in the dev stack — passwords and tokens would travel in cleartext without TLS.
3. **No token revocation or refresh.** A stolen JWT works until it expires; there's no password-change or logout endpoint.
4. **No email verification or password reset.**
5. **Admin bootstrapping is manual.** The only way to get an admin is the seed script (or editing the database). There is deliberately no API to grant admin, but also no admin-management tooling and no audit log of admin actions.
6. **Isolation is enforced in application code.** Every tool and query filters by `customer_id`. Consider Postgres Row-Level Security as defence in depth.
7. **Symmetric JWT key** (HS256, single shared secret).
8. **Tool results reach the model as untrusted data** (e.g. a ticket's own `description` field). The system prompt tells the model not to follow instructions found inside them; this is a prompt-level mitigation, not an enforced guarantee. No tool currently returns free text a customer doesn't already own, which limits the blast radius today, but this needs re-evaluating before any tool exposes other customers' free-text fields (e.g. shared ticket threads).

**Store back-end**

9. **No payments.** Orders are records only; nothing charges anyone, and `pending → processing → completed` is driven manually by an admin.
10. **Ordering doesn't change `customers.plan`.** Buying "Pro plan" leaves the customer's `plan` field as it was; plans and products aren't linked.
11. **No shipping/address, tax, discounts, currency or refunds.** Everything is a single implicit currency.
12. **Customers can't edit or delete orders/tickets** beyond cancelling/closing; there are no ticket comments/replies.
13. **Exact stock levels are public** (`stock_quantity` on product reads, and `search_products`'s `units_left`). Hide it behind `in_stock`/`availability` only if inventory is sensitive.
14. **No stock reservation timeout.** Stock is decremented at order time; abandoned `pending` orders keep it held until cancelled.
15. **Legacy products.** Migrated orders point at inactive "legacy" products; they can't be deleted (409) and clutter `include_inactive` listings.
16. **Migration `0002` is irreversible** and its CHECK constraints would fail on a database that already holds unexpected status values.
17. **Migrations run at app startup.** Fine for one instance; with several, run them as a separate deploy step to avoid races.

**Chatbot**

18. **The model sometimes skips an available tool.** Observed live: a product-pricing question that happened to also be covered by the retrieved FAQ text was answered from the FAQ without calling `search_products`, even with the tool offered. The always-included FAQ context gives the model an escape hatch from calling a tool for data that should authoritatively come from the live table. If database data must always win for a given topic, that topic likely needs to be removed from `faq.md` so the tool is the only source.
19. **`sources` can be misleading** — the top-`k` FAQ chunks are always retrieved and listed regardless of whether the final answer used them or came entirely from a tool.
20. **Signed-out gate is regex-based.** A signed-out user can phrase an account question without trigger phrases (confirmed live, §10.2); this is **not** a data leak — no account tool is ever offered to an anonymous caller — just a less helpful answer than the canned login prompt.
21. **No conversation memory.** Each `/chat` call is independent, including the tool-calling loop within it; a follow-up question starts over.
22. **Retrieval ranking on short generic queries** with the small embedding model can put the best FAQ chunk 2nd–3rd.
23. **The model can still misread, omit, or (per §10.3) simply not call a tool for data it should have looked up.** The live scenarios in §10.2 were judged by hand, not asserted by tests. Consider the planned confidence/groundedness checks and an answer-quality test set that specifically checks *which* tool was called, not just whether the final answer sounds right.
24. **The LLM is billed per token, and tool calling costs noticeably more per question than the old single-call design** (each round-trip resends the full history and every tool schema). Budget accordingly; watch for `402`.
25. **Tool-calling reliability is provider-dependent, not just model-dependent** (§10.3) — the same model id can behave differently, or not support tools at all, depending which provider serves it. Re-verify against the live router before changing `LLM_MODEL`/`LLM_PROVIDER`.

**Engineering**

26. **`VectorKnowledgeBase` still opens its own psycopg2 connections** per call (the ORM layer is pooled).
27. **Full FAQ re-index on every startup.**
28. **Dev-mode container** (`--reload`, whole-project bind mount).
29. **Demo credentials** are random and printed once; the seed is for local demo data only.
30. **No CI** — the new pytest suite runs locally/manually only; it isn't wired into any pipeline yet.

---

## 13. Suggested Next Steps

1. **Rate limiting / lockout** on login and registration; **TLS**.
2. **More tools, carefully** — `cancel_order`/`create_ticket` are natural next tools, but as mutating actions they need an explicit confirmation step before executing, unlike the read-only tools here.
3. **Wire the pytest suite into CI**, and extend it to the REST API's 97 existing manual checks so they stop being an ad-hoc script.
4. **Payments and order flow** — a payment provider, and tie a purchased plan to `customers.plan`.
5. **Admin tooling** — manage admins, audit log of product/order/tool actions.
6. **Router + confidence/groundedness check** before answering; escalate low-confidence answers to a human. Would also help catch the "skipped an available tool" failure mode in limitation 18.
7. **Conversation history** for follow-up questions.
8. **Password change/reset, email verification, token refresh/revocation.**
9. **Row-Level Security**, connection pooling for the vector store, and a production container image.
