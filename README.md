# Support Bot Starter (RAG orchestration project)

A minimal retrieval-augmented support chatbot exposed as an HTTP API:
it answers customer questions using your own product docs *and* live
per-customer records (orders, accounts, tickets) from Postgres, and
says "I don't know" instead of guessing when neither source covers
something.

For the technologies used (LLM, embeddings, vector store, auth, ...) and their
pros and cons, see [TECH_STACK.md](TECH_STACK.md); for a full walkthrough of
what was built, see [DOCUMENTATION.md](DOCUMENTATION.md).

## What's here

```
support-bot/
├── app/
│   ├── main.py                  # FastAPI app + startup (migrations, KB index, clients)
│   ├── config.py                # reads HF_TOKEN / DATABASE_URL / JWT_SECRET from the environment
│   ├── db/                      # SQLAlchemy Base, pooled engine/session, migration runner
│   ├── models/                  # ORM models: Customer, ProductType, Product, Order, OrderItem, Ticket
│   ├── api/
│   │   ├── deps.py              # bearer token -> customer; require_admin; DB session
│   │   ├── routes/              # function-based routes
│   │   │   ├── auth.py          # POST /auth/register, /auth/login; GET /auth/me
│   │   │   ├── chat.py          # POST /chat
│   │   │   ├── chat_ws.py       # WebSocket /ws/chat (stateful conversation)
│   │   │   ├── web.py           # GET / -> the browser chat UI
│   │   │   └── health.py        # GET /health
│   │   └── views/               # CLASS-BASED views (one class per resource)
│   │       ├── base.py          # APIView + @route helper
│   │       ├── product_types.py # ProductTypeView (categories + their attribute definitions)
│   │       ├── products.py      # ProductView
│   │       ├── orders.py        # OrderView
│   │       └── tickets.py       # TicketView
│   ├── web/index.html           # chat UI (plain HTML/JS; talks to the API over REST or WebSocket)
│   ├── schemas/                 # pydantic request/response models (auth, chat, product, order, ticket)
│   └── services/
│       ├── document_loader.py       # chunks .md/.txt files into retrievable pieces
│       ├── embeddings.py            # local embedding model (fastembed) for semantic search
│       ├── vector_knowledge_base.py # pgvector-backed semantic retrieval over FAQ docs
│       ├── auth.py                  # argon2 password hashing + JWT create/verify
│       ├── orders.py                # order rules: price snapshot, stock, status transitions
│       ├── errors.py                # domain errors -> HTTP statuses
│       ├── database.py              # ORM-backed helpers for chat/login (customer data scoped by customer_id)
│       ├── llm.py                   # HF chat-completion client: model/provider, retry, 503 on failure
│       ├── tools.py                 # TOOL CALLING: schemas + handlers the model may invoke
│       ├── catalog.py               # product attributes checked against their type's definition
│       ├── customer_lookup.py       # signed-out gate only (spots account-type questions)
│       └── chat_service.py          # retrieve FAQ -> tool-calling loop -> answer
├── knowledge/
│   └── faq.md                   # sample company FAQ — replace with your own docs
├── migrations/                  # Alembic migrations (source of truth for the schema)
├── alembic.ini
├── seed_data.py                 # migrates, then adds demo admin/customers/products/orders (random passwords, printed once)
├── tests/                       # pytest: tool-calling loop + WebSocket chat, against a scripted fake model
├── Dockerfile                   # image for the API
├── docker-compose.yml           # api + Postgres, wired together
├── requirements.txt
├── requirements-dev.txt         # + pytest, for running tests/
└── README.md
```

## Setup (Docker, recommended)

Runs Postgres and the API together in containers — no local Postgres
install needed.

1. Copy the env template and fill in your secrets:
   ```
   cp .env.example .env
   ```
   - `HF_TOKEN` -- a token from https://huggingface.co/settings/tokens
     (needs the "Make calls to Inference Providers" permission). **This is
     billed against your HF account's credits** -- despite the "Inference
     Providers" branding, no provider on the router serves any model for
     free (checked against the live listing at
     https://router.huggingface.co/v1/models). A `402 Payment Required`
     from the API means the account is out of credit.
   - `HF_FALLBACK_TOKENS` (optional) -- more tokens, comma-separated, e.g.
     from a second HF account. If a token is expired/invalid (401), out of
     credit (402), not permitted (403) or rate-limited (429), the same
     request is retried with the next token, and the failed one is skipped
     for an hour (a minute for rate limits) before being tried again. The
     server log names which token failed (by its last 4 characters only).
     Provider outages (5xx) don't switch tokens -- they'd fail on any.
   - `GROQ_API_KEY`, `GEMINI_API_KEY`, `CEREBRAS_API_KEY`,
     `OPENROUTER_API_KEY` (all optional) -- other providers with a free
     tier, used in that order once every HF token has failed or Hugging
     Face itself is erroring. Each has a default model, overridable with
     `<NAME>_MODEL` (it must support tool calling); see `.env.example`.
   - `JWT_SECRET` -- signs login tokens; at least 32 characters. Generate one:
     `python -c "import secrets; print(secrets.token_urlsafe(48))"`
   - `LLM_MODEL` / `LLM_PROVIDER` (optional) -- default to
     `Qwen/Qwen2.5-72B-Instruct` on `deepinfra`. Tool calling needs a
     model+provider pair where the provider actually supports it -- check
     `supports_tools` for that pair at the router URL above before
     changing either. (`microsoft/phi-4`, this project's original model,
     does not support tool calling on any current provider.)

   The app refuses to start without a valid `JWT_SECRET`.
   `DATABASE_URL` in `.env` is ignored when using Docker -- compose
   points the API at its own `db` container automatically.

2. Start everything:
   ```
   docker compose up -d
   ```

3. Add the demo data (one-off; the tables themselves are created
   automatically at startup by the migrations):
   ```
   docker compose run --rm app python seed_data.py
   ```
   It creates an **admin** (`admin@example.com`), two customers (Ada and
   Grace), a catalog of **6 product types** (digital services, electronics,
   clothing, books, home & kitchen, gift cards) with **30 products**, and a
   few orders and tickets. Re-running it only adds what is missing. It prints a
   random password for each new account **once** -- copy them. There are no
   default credentials. (To get fresh ones, run `docker compose down -v` and
   seed again.)

4. The API is now listening at http://localhost:8020 (mapped from the
   container's port 8000 -- change the host side in `docker-compose.yml`
   if 8020 is taken on your machine too). Try it:
   ```
   # general FAQ question -- no login needed
   curl -X POST http://localhost:8020/chat \
     -H "Content-Type: application/json" \
     -d '{"question": "What plans do you offer?"}'

   # log in (form fields; "username" is your email)
   curl -X POST http://localhost:8020/auth/login \
     -d "username=ada@example.com&password=<password from seed step>"

   # ask about your own account with the returned access_token
   curl -X POST http://localhost:8020/chat \
     -H "Content-Type: application/json" \
     -H "Authorization: Bearer <access_token>" \
     -d '{"question": "What is the status of order 1?"}'
   ```
   Or open **http://localhost:8020/** for the chat UI: sign in (or create
   an account) and chat, switching between **WebSocket** and **REST** in the
   header. It only uses the public API below, so it doubles as an example
   client.

   Or open http://localhost:8020/docs for interactive Swagger UI: click
   **Authorize**, log in with the same email/password, and every request
   you try afterwards is sent as that customer.

5. Questions to try:
   - Signed out: "How do I reset my password?" (answered from the FAQ)
   - Signed out: "What's the status of order 1?" -- not answered; the API
     returns `needs_authentication: true` and never calls the model.
   - Signed in as Ada: "What's the status of order 1?" (her order -- answered)
   - Signed in as Ada: "What's the status of order 2?" (Grace's order -- the
     bot says it isn't on her account, exactly as for a nonexistent order)
   - "Do you support Windows Phone?" (should say it doesn't know — good, that's the point)
   - Anyone: "Do you sell a backup drive and how many are in stock?" (calls
     `search_products` -- check `tools_used` in the response)
   - Anyone: "What kinds of products do you sell?" (calls `list_product_types`)
   - Anyone: "Do you have any books by Maya Rivera?" / "Show me clothing under
     $30 that is in stock" (search by attribute values and category)
   - Anyone: "Is the USB-C hub available?" (it's seeded as sold out -- the bot
     says so rather than claiming it doesn't exist)
   - Signed in: "I'd like 2 black logo T-shirts in M and the Backup Handbook" --
     the bot quotes the items and total (`prepare_order`) and asks you to
     confirm; reply "yes" and it places the order (`confirm_order`) and gives
     you the order number.
   - Signed in: "Please cancel order 5" -- the bot shows the order and asks you
     to confirm (`request_cancellation`); reply "yes, cancel it" and it's
     cancelled (`confirm_cancellation`). Only `pending` orders can be.
   - As **admin**, `PATCH /products/{id}` with `{"price": "7.50"}` (or set
     `stock_quantity` to `0`), then ask the bot again -- its answer changes
     immediately, because the tool reads the table on every call.
   - As Ada, `POST /orders` for the drive, then ask "What have I ordered?"
     (calls `list_my_orders`)
   - As Ada, ask about order 2 (Grace's order): the tool returns "not found,"
     identical to asking about a nonexistent order.

The container runs `uvicorn --reload` with the project mounted as a
volume, so code edits take effect immediately without a rebuild.
Rebuild only when `requirements.txt` changes:
```
docker compose build app
```

**Run the tests** (a scripted fake model, so this costs no HF credits and
needs no Postgres):
```
docker compose run --rm app sh -c "pip install -q -r requirements-dev.txt && python -m pytest -q"
```

If you ever want to inspect the database directly, exec into the
container:
```
docker compose exec db psql -U supportbot -d supportbot
```

## Setup (without Docker)

1. Install dependencies:
   ```
   pip install -r requirements.txt
   ```

2. Set up Postgres yourself and put its connection string in
   `DATABASE_URL` in `.env`, along with `HF_TOKEN` and `JWT_SECRET`
   (see `.env.example`).

3. Create the demo customers:
   ```
   python seed_data.py
   ```

4. Run the API:
   ```
   uvicorn app.main:app --reload
   ```

## The API

### Authentication

Account data is tied to who you **log in as**, never to an email or order
number typed into a question. Signed-out callers can still ask general FAQ
questions; anything that needs account data requires a bearer token.

**`POST /auth/register`** -- JSON `{"name", "email", "password"}` (password
8-128 chars). Creates a new customer on the `free` plan; returns the profile
(`201`). An email that already exists returns `409` and is never overwritten,
so nobody can claim an existing customer's account. Emails are trimmed and
lower-cased.

**`POST /auth/login`** -- OAuth2 password flow: form fields `username` (your
email) and `password`. Returns:
```json
{ "access_token": "<jwt>", "token_type": "bearer", "expires_in": 3600 }
```
A wrong password, an unknown email, and an account without a password all
return the same `401 Incorrect email or password`.

**`GET /auth/me`** -- returns the signed-in customer's profile (`401` without a
valid token). Handy for checking a token.

Send the token on later requests as `Authorization: Bearer <access_token>`.
Tokens expire after `JWT_EXPIRE_MINUTES` (default 60); a missing, expired,
tampered, or otherwise invalid token gets `401` (an invalid token is never
quietly treated as "signed out").

### Chat

**`POST /chat`** -- JSON `{"question": "...", "history": [...]}`. Stateless:
`history` (optional) is the earlier turns, oldest first, as
`{"question", "answer"}` objects; the most recent 6 are given to the model so
follow-ups like "how much is it?" work. Returns `answer`, `sources`,
`tools_used` and `needs_authentication`.

**`WS /ws/chat`** -- one connection is one conversation; the server keeps the
history. JSON text frames:

| Client sends | Server replies |
|---|---|
| *(on connect)* | `{"type": "ready", "customer": null}` |
| `{"type": "auth", "token": "<access_token>"}` (`null` signs out) | `{"type": "auth_ok", "customer": {...}}` or an `unauthorized` error |
| `{"type": "message", "question": "..."}` | `{"type": "typing"}`, then `{"type": "delta", "text": "..."}` frames as the answer is written, then `{"type": "answer", ...}` (same fields as `POST /chat`) with the full answer, which replaces the deltas. A second `typing` means the deltas so far should be dropped |
| `{"type": "reset"}` | `{"type": "reset_ok"}` -- history forgotten |
| `{"type": "ping"}` | `{"type": "pong"}` |

Errors arrive as `{"type": "error", "code", "detail"}` with `code` one of
`unauthorized`, `invalid_frame`, `service_unavailable`, `internal_error`; the
connection stays open. The token is sent in a frame (not the URL, so it stays
out of access logs) and re-checked before every question: once it expires
you get `unauthorized` and are signed out, never silently answered as
anonymous. Signing in as someone else clears the history.

```
# quick try with websocat (https://github.com/vi/websocat)
websocat ws://localhost:8020/ws/chat
{"type": "message", "question": "Do you sell a backup drive?"}
{"type": "message", "question": "How many are in stock?"}
```

### Ordering and cancelling through the chat

The bot can place orders, in two steps, with the safety checks done by the
server (`app/services/order_drafts.py`), not just asked of the model:

1. `prepare_order` saves a **draft** (a quote: items, current prices, total)
   in the `order_drafts` table. Nothing is ordered or reserved.
2. `confirm_order` places the customer's open draft through the same
   `create_order` as `POST /orders`, and is refused unless:
   - the draft was prepared in an **earlier** message (the customer has seen
     the summary -- quote and confirm can't happen in one turn);
   - the customer's **current message is a clear yes** ("yes", "ok, place
     it" ...; anything with "no", "wait", "change" etc. is not);
   - the draft is less than **15 minutes** old;
   - the total is **unchanged** (a price change means a new quote).

**The order form (web chat).** When the bot prepares an order, the chat
answer also carries an `order_form` (in `POST /chat` and the WebSocket
`answer` frame): each line with its **options** -- the product's active
variants (or just the product, if it has none) -- with their prices and how
many can be ordered. If the bot names the product rather than a variant, an
in-stock variant is preselected. The chat page shows it as a card:
pick a variant, enter a quantity (capped at its stock), see the line and order
totals update, then **Confirm order** or **Cancel**:
```
POST /orders/drafts/{draft_id}/confirm
{ "items": [{"sku": "TSHIRT-LOGO-BLK-L", "quantity": 2}], "expected_total": "48.00" }
POST /orders/drafts/{draft_id}/discard
```
A click by the signed-in customer is itself the confirmation (so no "earlier
message" rule here). `expected_total` is the total the form displayed: if
current prices give a different total, nothing is ordered (`409`) -- the
customer never pays a total they didn't see. A form can be used once, only
by its customer, for 15 minutes, and a newer quote retires older forms.
Replying "yes" in the chat still works too (for text-only clients).

A new quote replaces the customer's previous open draft, a draft can only be
placed once (row lock), and stock is re-checked when placing. Orders placed
this way are normal `pending` orders, so the customer can still cancel them.
Ordering needs a signed-in customer; the confirmation wording check is
English-only.

**Cancelling** works the same way (`app/services/order_cancellations.py`):
`request_cancellation(order_id)` checks the order is the customer's own and
still `pending`, records the request (table `cancellation_requests`) and shows
the order; `confirm_cancellation` cancels it -- returning the items to stock --
only if the request came in an earlier message, the current message is a
clear yes ("yes, cancel it"; "no, keep it" is not), it's under 15 minutes
old, and the order is still pending. Processing or completed orders get an
explanation instead (contact support / returns policy). The bot only ever
touches the signed-in customer's **own** orders -- even an admin chatting
with it can't cancel other customers' orders through the bot.

### Products, orders and tickets

These are **class-based views** (`app/api/views/`) over Postgres tables. Who can
do what:

| Endpoint | Signed out | Customer | Admin |
|---|---|---|---|
| `GET /product-types`, `GET /product-types/{id}` | active types | active types | + inactive (`?include_inactive=true`) |
| `POST /product-types`, `PATCH /product-types/{id}`, `DELETE /product-types/{id}` | 401 | 403 | yes |
| `GET /products`, `GET /products/{id}` | active products | active products | + inactive (`?include_inactive=true`) |
| `POST /products`, `PATCH /products/{id}`, `DELETE /products/{id}` | 401 | 403 | yes |
| `POST /orders` | 401 | place an order for **yourself** | yes (as themselves) |
| `GET /orders`, `GET /orders/{id}` | 401 | **your own** only | everyone's (`?customer_id=`) |
| `POST /orders/{id}/cancel` | 401 | your own, only while `pending` | any cancellable order |
| `PATCH /orders/{id}/status` | 401 | 403 | yes (`pending`→`processing`→`completed`, or `cancelled`) |
| `POST /tickets`, `GET /tickets`, `GET /tickets/{id}` | 401 | your own | everyone's |
| `POST /tickets/{id}/close` | 401 | your own | yes |
| `PATCH /tickets/{id}/status` | 401 | 403 | yes |

Other people's orders and tickets return `404` -- identical to a nonexistent id.

**Product types** -- every product belongs to one type (books, clothing, ...),
and the type defines which **attributes** its products carry:
```json
POST /product-types
{
  "slug": "clothing", "name": "Clothing", "description": "Acme apparel.",
  "attribute_schema": [
    {"key": "size",  "label": "Size",   "type": "choice", "required": true, "options": ["S", "M", "L"]},
    {"key": "color", "label": "Colour", "type": "text",   "required": true},
    {"key": "weight_g", "label": "Weight", "type": "integer", "unit": "g"}
  ]
}
```
Attribute `type` is `text`, `integer`, `number`, `boolean` or `choice` (which
needs `options`). `slug` is lower-case words joined by hyphens. Rules:
- Changing a type's `attribute_schema` is refused (`409`) if any existing
  product of that type wouldn't match the new one -- fix those products first.
- A type that still has products can't be deleted (`409`); deactivate it
  instead (`PATCH {"is_active": false}`). An inactive type is hidden from the
  listing and can't receive new products; its existing products stay as they
  are (each product's own `is_active` controls whether it's for sale).
- Products that existed before types were introduced were put in a `general`
  type with no attributes (the seed script moves its own demo products out).

**Products** -- `{sku, product_type_id, name, description, attributes, price,
billing_period, stock_quantity, is_active}`. `attributes` must match the
product type: required attributes present, no unknown keys, values of the
declared kind (otherwise `422`, listing every problem). Changing a product's
type usually means sending new `attributes` in the same `PATCH`. Responses
include `product_type: {id, slug, name}` and a computed `in_stock`.
`billing_period` is `one_time`, `monthly` or `yearly`. `stock_quantity: null`
means unlimited (digital goods); a number is units left. List supports
`?q=` search (name/description/SKU), `?type=<slug>`, `?min_price=`,
`?max_price=`, `?in_stock=true`, `?limit=` (1-100) and `?offset=`.

**Variants** -- sizes, colours, formats... are stored as a JSON list in the
product's `variants` column (Postgres JSONB):
```json
POST /products
{
  "sku": "TSHIRT-LOGO", "product_type_id": 4, "name": "Acme logo T-shirt", "price": "24.00",
  "attributes": {"material": "Organic cotton", "fit": "Unisex"},
  "variants": [
    {"sku": "TSHIRT-LOGO-BLK-M", "options": {"size": "M", "color": "Black"}, "stock_quantity": 30},
    {"sku": "TSHIRT-LOGO-NVY-L", "options": {"size": "L", "color": "Navy"}, "price": "26.00", "stock_quantity": 6}
  ]
}
```
- Which attributes vary is set on the **product type**: attribute definitions
  with `"variant": true` (e.g. clothing `size`/`color`, books `format`,
  electronics `capacity`/`model`/`color`, gift cards `amount`/`delivery`). A
  product with variants sets those in each variant's `options` and only the
  other attributes on itself; a product without variants sets all of them on
  itself, as before.
- Each variant has its **own SKU, stock and (optionally) price** (`null` =
  the product's price). The product's own `stock_quantity` must be `null`
  once it has variants. Responses add `in_stock` per variant, and the
  product's `in_stock` is true if any active variant is.
- SKUs are unique across all products **and** variants (`409` otherwise).
  Postgres can't enforce uniqueness inside a JSON array, so the app checks it
  under per-SKU advisory locks; a GIN index on `variants` keeps the lookup fast.
- `PATCH` with `variants` replaces the whole list. To stop selling a variant,
  set its `is_active: false` (removing it is allowed -- order lines keep a
  snapshot -- but cancelling such an order can't put its stock back).
- **Ordering**: `POST /orders` items take `variant_sku` -- required for a
  product with variants (`422` lists the choices). Order lines record
  `variant_sku` and a name like `Acme logo T-shirt (Size: M, Colour: Black)`.
  Stock is taken from, and on cancel returned to, that variant.
- `?in_stock=true` and the bot's search look inside the variants; `?q=` and
  the bot's search also match variant SKUs and options.

The seed script converted the earlier one-product-per-size items (e.g. the
separate `TSHIRT-LOGO-BLK-M` / `-L` products) into variants of one product,
carrying their stock over; the old products stay, inactive and renamed
`...-OLD<id>`, because past orders point at them.
Deleting a product that appears on an order returns `409` -- deactivate it
instead (`PATCH {"is_active": false}`).

**Placing an order** -- send only product ids and quantities:
```json
POST /orders
{ "items": [ {"product_id": 6, "quantity": 2}, {"product_id": 2, "quantity": 1} ] }
```
The server takes the price from the product table, computes the total, sets
`status: pending`, and decrements stock -- all in one transaction, so if any
line is out of stock (`409`) nothing is saved. Line items keep a copy of the
product's name and price at purchase time, so later price changes don't
rewrite history. Cancelling returns the items to stock. Prices are JSON strings
(e.g. `"89.00"`) to avoid floating-point error.

All request bodies reject unknown fields (`422`), so a client cannot supply its
own `price`, `total_amount`, `status`, `customer_id` or `is_admin`.

### Chat (tool calling)

**`POST /chat`** -- the `Authorization` header is optional.

Request:
```json
{ "question": "What's the status of order 1?" }
```
(`question` is 1-2000 characters.)

Response:
```json
{
  "answer": "...",
  "sources": ["faq.md"],
  "needs_authentication": false,
  "tools_used": ["get_order"]
}
```

Instead of the server deciding up front what data to hand the model, the model
is given **tools** (`app/services/tools.py`) and decides for itself, per
question, whether to call one:

| Tool | What it does | Needs login |
|---|---|---|
| `search_products` | Search the live catalog by keywords (name, description, attribute values) / category / max price / in-stock | No |
| `list_product_types` | The store's categories, with product counts and the details each lists | No |
| `prepare_order` | Quote an order (SKUs + quantities): checks stock and prices, saves a draft, orders **nothing** | Yes |
| `confirm_order` | Place the customer's quoted order | Yes |
| `request_cancellation` | Check one of the customer's own orders can be cancelled and show it; cancels **nothing** | Yes |
| `confirm_cancellation` | Cancel that order (stock goes back) | Yes |
| `get_order` | Look up ONE of the signed-in customer's own orders by number | Yes |
| `list_my_orders` | The signed-in customer's own orders, optionally by status | Yes |
| `list_my_tickets` | The signed-in customer's own tickets, optionally by status | Yes |

| Caller | Question | Result |
|---|---|---|
| Signed out | general / FAQ | Answered from the FAQ |
| Signed out | product / pricing | May call `search_products` or `list_product_types`; those are the only tools it's offered |
| Signed out | mentions "my order", an order number, or an email | `needs_authentication: true`, a "please log in" message, no model call |
| Signed in | anything | FAQ chunks + whichever tools the model chooses to call, scoped to **that customer's own** data |
| Any | model provider busy or failing | `503` with a friendly message and `Retry-After` (one automatic retry on transient errors) |

`tools_used` lists which tools actually ran, for transparency -- it's normal
for it to be empty (the FAQ alone answered) or to contain more than one tool.

**Why this is still safe:** no tool's schema accepts a customer id, and every
account tool reads `customer["id"]` from the verified login token, never from
the model's arguments (`app/services/tools.py`'s module docstring spells out
the rule). A signed-out caller is never even offered the account tools, so
there's nothing for a cleverly-worded question to talk the model into calling.
Someone else's order number returns the same "not found" as one that doesn't
exist. Tools are read-only and every result is capped in size; see
`tests/test_tool_calling.py` for the checks (customer scoping, repeated-call
and step-count guards, error handling) run against a scripted fake model.

**`GET /health`** -- returns `{"status": "ok"}`, useful for container
health checks / load balancer probes.

## Using your own docs

Drop `.md` or `.txt` files into the `knowledge/` folder — one topic
per file works well, but it's not required. Restart the app and it
will re-embed and re-index everything into pgvector on startup
(`documents` is wiped and rebuilt from `knowledge/` each time, so
there's no separate migration step to remember).

Retrieval is semantic (embedding similarity), not keyword matching, so
a question phrased very differently from the docs' wording still finds
the right chunk -- e.g. "will my stuff get wiped if I stop paying?"
correctly retrieves the cancellation FAQ section even though it shares
almost no words with it.

## Data model, migrations and your own tables

The schema is defined by the ORM models in `app/models/` and created/changed by
**Alembic migrations** in `migrations/` (the app applies pending ones at
startup). To change a table:

```
# 1. edit the model in app/models/, then generate a migration
docker compose run --rm app alembic revision --autogenerate -m "add thing"
# 2. review migrations/versions/<new file>.py, then restart the app (or:)
docker compose run --rm app alembic upgrade head
# 3. optional: confirm models and database agree
docker compose run --rm app alembic check
```

Always read an autogenerated migration before applying it. The `documents`
(pgvector) table is deliberately excluded -- it is managed by
`vector_knowledge_base.py`.

To add a new REST resource: add a model, a schema in `app/schemas/`, and a
view class in `app/api/views/` (copy `tickets.py`), then register it in
`app/main.py`.

**Rule to keep when you extend it:** every query returning customer-owned
data takes the *authenticated* `customer_id` and filters on it (see
`get_order_for_actor` and `Database.get_order_for_customer`). Never look
records up by an id or email that came from the question text -- that is
exactly the hole authentication closes. And never give the model database
access or let it write SQL: the bot only ever sees rows that fixed,
parameterised queries chose for it.

**To add a new chat tool:** add an argument model, a handler and a `Tool(...)`
entry in `app/services/tools.py` (copy `list_my_orders`). The same rule
applies there: the handler reads `ctx.customer["id"]`, never an id from the
tool's arguments -- see that file's module docstring.

## How the orchestration works right now

```
        POST /chat  { question }   [+ Authorization: Bearer <jwt>]
                              │
                              ▼
                   app/api/routes/chat.py
        (api/deps.py: token -> customer, or None; bad token -> 401)
                              │
                              ▼
                app/services/chat_service.answer_question()
                              │
        signed out + account-type question?  ──yes──▶  "please log in"
                              │ no                     (no model call)
                              ▼
              vector_knowledge_base.retrieve()   (FAQ chunks, always)
                              │
                              ▼
              ┌─────────────────────────────────────────────┐
              │  LOOP (up to 4 rounds), tools_for(customer)  │
              │                                              │
              │   model call ──tool_calls?──yes──┐           │
              │       ▲                          ▼           │
              │       │              run_tool() with the     │
              │       │              AUTHENTICATED customer,  │
              │       │              never an id from the     │
              │       │              model's arguments        │
              │       │                          │            │
              │       └────── append result, loop back ───────┘
              │                                              │
              │   model call ──tool_calls?──no───▶ done: answer text
              └─────────────────────────────────────────────┘
                              ▼
       { answer, sources, needs_authentication, tools_used }
```

Because a tool can only be called with the caller's own `customer_id` --
never one the model supplies -- another customer's rows are never fetched, so
no prompt, however cleverly worded, can make the model reveal them. The
system prompt also tells the model to decline questions about other
accounts, but the real protection is that the tool would refuse (or the tool
isn't even offered to a signed-out caller) regardless of what the model asks.

This is a real (if simple) agentic RAG pipeline: the model decides what it
needs and asks for it through a fixed, read-only set of tools -- it never
touches the database or writes SQL. Because tools query Postgres live,
editing a product or placing an order through the API changes the bot's very
next answer.

## Next steps (in order of value)

1. **More tools** — mutating actions (`cancel_order`, `create_ticket`) are a
   natural extension of `app/services/tools.py`, but unlike the read-only
   tools here they cause side effects and should require an explicit
   confirmation step before executing, not run on the model's say-so alone.

2. **Router step** — before retrieving, ask the model to classify the
   question (product / billing / off-topic / complaint). Route
   billing questions to a different prompt or escalate complaints
   immediately instead of trying to answer them.

3. **Confidence check** — after getting an answer, ask the model a
   second time: "Given the context, how confident are you in this
   answer: high/medium/low?" If low, reply with an escalation message
   instead of the guessed answer.

4. **Logging** — write every (question, answer, sources, confidence)
   tuple to a CSV or a new Postgres table.

5. **Insight report** — a separate script you run daily/weekly that
   feeds the whole log to the model and asks: "What are customers
   asking about most? What questions got low-confidence answers?"
   This is the "insights" layer that makes the bot valuable beyond
   just answering questions one at a time.

6. **Harden authentication** — login/registration are in place, but for
   production add: rate limiting / lockout on `/auth/login` and
   `/auth/register` (nothing stops password guessing yet), email
   verification and password reset, token revocation (a stolen token is
   valid until it expires), and HTTPS in front of the API.

7. **Better/bigger embedding model** — `bge-small-en-v1.5` (the current
   default in `app/services/embeddings.py`) is fast and good enough for
   a handful of FAQ docs, but its similarity scores cluster in a fairly
   narrow band on short generic questions, which can put the best match
   2nd or 3rd instead of 1st (still fine at `k=3`, but worth watching as
   your knowledge base grows). A bigger model (`bge-base-en-v1.5`) or a
   reranking step after retrieval would sharpen this.

8. **UI** — build a small frontend (or a Streamlit app) that calls
   `POST /chat` instead of curl/Swagger, for a real chat interface.
