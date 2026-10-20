"""
app/services/database.py

Read/write helpers used by the chat and login flows, built on the ORM
models and a pooled SQLAlchemy engine. They return plain dicts so callers
(context builders, auth routes) don't depend on ORM objects or sessions.

The REST resource views (products / orders / tickets) use ORM sessions
directly -- see app/api/views.

Every accessor for customer-owned data requires a customer_id, and callers
pass the *authenticated* customer's id, never one taken from question text.
That is what stops one customer reading another's records.
"""

import sqlalchemy as sa
from sqlalchemy import func, or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import sessionmaker

from app.models import Customer, Order, Product, ProductType, Ticket
from app.services import conversations
from app.services import variants as variant_rules
from app.services.catalog import describe_attributes
from app.services.order_cancellations import confirm_cancellation, request_cancellation
from app.services.order_drafts import confirm_draft, prepare_cart, prepare_draft

# Cap on rows fed into the model's context per list.
MAX_ROWS = 20


def _order_dict(order: Order) -> dict:
    return {
        "id": order.id,
        "status": order.status,
        "total_amount": order.total_amount,
        "created_at": order.created_at,
        "items": [
            {
                "product_name": i.product_name,
                "unit_price": i.unit_price,
                "quantity": i.quantity,
            }
            for i in order.items
        ],
    }


class Database:
    def __init__(self, session_factory: sessionmaker):
        self._session_factory = session_factory

    def get_customer_by_id(self, customer_id):
        """Public profile only -- never returns password_hash."""
        with self._session_factory() as session:
            row = session.execute(
                select(Customer.id, Customer.name, Customer.email, Customer.plan, Customer.is_admin)
                .where(Customer.id == customer_id)
            ).mappings().first()
            return dict(row) if row else None

    def get_customer_auth_record(self, email):
        """Includes password_hash. Only for the login flow."""
        with self._session_factory() as session:
            row = session.execute(
                select(
                    Customer.id, Customer.name, Customer.email, Customer.plan,
                    Customer.is_admin, Customer.password_hash,
                ).where(Customer.email == email)
            ).mappings().first()
            return dict(row) if row else None

    def create_customer(self, name, email, password_hash):
        """Insert a new (non-admin) customer. Returns the profile, or None if
        the email is already registered (never overwrites an existing row)."""
        with self._session_factory() as session:
            row = session.execute(
                pg_insert(Customer)
                .values(name=name, email=email, password_hash=password_hash)
                .on_conflict_do_nothing(index_elements=[Customer.email])
                .returning(Customer.id, Customer.name, Customer.email, Customer.plan, Customer.is_admin)
            ).mappings().first()
            session.commit()
            return dict(row) if row else None

    def get_order_for_customer(self, order_id, customer_id):
        """None both when the order doesn't exist and when it belongs to
        someone else, so callers can't tell the difference."""
        with self._session_factory() as session:
            order = session.scalar(
                select(Order).where(Order.id == order_id, Order.customer_id == customer_id)
            )
            return _order_dict(order) if order else None

    def get_orders_for_customer(self, customer_id, status=None, limit=MAX_ROWS):
        with self._session_factory() as session:
            stmt = select(Order).where(Order.customer_id == customer_id)
            if status is not None:
                stmt = stmt.where(Order.status == status)
            orders = session.scalars(
                stmt.order_by(Order.created_at.desc(), Order.id.desc()).limit(limit)
            ).all()
            return [_order_dict(o) for o in orders]

    def get_tickets_for_customer(self, customer_id, status=None, limit=MAX_ROWS):
        with self._session_factory() as session:
            stmt = select(Ticket).where(Ticket.customer_id == customer_id)
            if status is not None:
                stmt = stmt.where(Ticket.status == status)
            tickets = session.scalars(
                stmt.order_by(Ticket.created_at.desc(), Ticket.id.desc()).limit(limit)
            ).all()
            return [
                {
                    "id": t.id,
                    "subject": t.subject,
                    "status": t.status,
                    "description": t.description,
                    "created_at": t.created_at,
                }
                for t in tickets
            ]

    def list_product_types(self):
        """Active product types with how many active products each has."""
        with self._session_factory() as session:
            count = (
                select(func.count(Product.id))
                .where(Product.product_type_id == ProductType.id, Product.is_active.is_(True))
                .scalar_subquery()
            )
            rows = session.execute(
                select(ProductType, count)
                .where(ProductType.is_active.is_(True))
                .order_by(ProductType.name)
            ).all()
            return [
                {
                    "slug": t.slug,
                    "name": t.name,
                    "description": t.description,
                    "attribute_schema": t.attribute_schema,
                    "product_count": n,
                }
                for t, n in rows
            ]

    def search_products(self, terms, category=None, max_price=None, in_stock_only=False, limit=10):
        """Active products only (public store data). `terms` are already
        cleaned keywords; a product matches if ANY term appears in its name,
        description, SKU, attribute values or type name, and products
        matching MORE terms rank first (so "backup drive" finds the drive
        before a plan that merely mentions backups), then cheapest first.
        `category` restricts to one product type (slug or name).
        Returns (rows, total_matches)."""
        with self._session_factory() as session:
            stmt = (
                select(Product)
                .join(Product.product_type)
                .where(Product.is_active.is_(True))
            )
            order = [Product.price, Product.name, Product.id]
            if terms:
                attribute_text = sa.cast(Product.attributes, sa.Text)
                variant_text = sa.cast(Product.variants, sa.Text)
                matches = [
                    or_(
                        Product.name.icontains(t, autoescape=True),
                        Product.description.icontains(t, autoescape=True),
                        Product.sku.icontains(t, autoescape=True),
                        attribute_text.icontains(t, autoescape=True),
                        variant_text.icontains(t, autoescape=True),
                        ProductType.name.icontains(t, autoescape=True),
                    )
                    for t in terms
                ]
                stmt = stmt.where(or_(*matches))
                score = sum(sa.case((m, 1), else_=0) for m in matches)
                order.insert(0, score.desc())
            if category:
                stmt = stmt.where(
                    or_(
                        ProductType.slug.icontains(category, autoescape=True),
                        ProductType.name.icontains(category, autoescape=True),
                    )
                )
            if max_price is not None:
                stmt = stmt.where(Product.price <= max_price)
            if in_stock_only:
                stmt = stmt.where(variant_rules.in_stock_clause())

            total = session.scalar(stmt.with_only_columns(func.count(Product.id)))
            products = session.scalars(stmt.order_by(*order).limit(limit)).all()
            rows = [
                {
                    "sku": p.sku,
                    "name": p.name,
                    "type": p.product_type.name,
                    "description": p.description,
                    "attributes": describe_attributes(p.product_type.attribute_schema, p.attributes),
                    "price": p.price,
                    "billing_period": p.billing_period,
                    "stock_quantity": p.stock_quantity,
                    "variants": [
                        {
                            "sku": v["sku"],
                            "options": variant_rules.label(p, v),
                            "price": variant_rules.unit_price(p, v),
                            "stock_quantity": variant_rules.stock_of(p, v),
                        }
                        for _, v in variant_rules.sellable_items(p) if v is not None
                    ],
                }
                for p in products
            ]
            for row in rows:
                stocks = [v["stock_quantity"] for v in row["variants"]] or [row["stock_quantity"]]
                row["available"] = any(s is None or s > 0 for s in stocks)
            return rows, total

    def load_conversation_history(self, customer_id, conversation_id, turns):
        """The last `turns` exchanges of the customer's own conversation, or
        None if there is no such conversation on their account."""
        with self._session_factory() as session:
            return conversations.history(session, customer_id, conversation_id, turns)

    def save_conversation_turn(
        self, customer_id, conversation_id, question, answer, sources, tools_used, now
    ):
        """Save one exchange (see conversations.add_turn). Returns the
        conversation's id, which is new if conversation_id wasn't usable."""
        with self._session_factory() as session:
            conversation = conversations.add_turn(
                session, customer_id, conversation_id, question, answer, sources, tools_used, now
            )
            session.commit()
            return conversation.id

    def prepare_order_draft(self, customer_id, lines, now):
        """Quote an order for the customer (see order_drafts.prepare_draft)."""
        with self._session_factory() as session:
            result = prepare_draft(session, customer_id, lines, now)
            session.commit()
            return result

    def prepare_cart(self, customer_id, skus, now):
        """A cart form for products the customer was shown (see order_drafts.prepare_cart)."""
        with self._session_factory() as session:
            form = prepare_cart(session, customer_id, skus, now)
            session.commit()
            return form

    def confirm_order_draft(self, customer_id, request_started_at, now):
        """Place the customer's open draft (see order_drafts.confirm_draft)."""
        with self._session_factory() as session:
            result = confirm_draft(session, customer_id, request_started_at, now)
            session.commit()
            return result

    def request_order_cancellation(self, customer_id, order_id, now):
        """Ask to cancel one of the customer's orders (see order_cancellations)."""
        with self._session_factory() as session:
            result = request_cancellation(session, customer_id, order_id, now)
            session.commit()
            return result

    def confirm_order_cancellation(self, customer_id, request_started_at, now):
        """Cancel the order the customer was asked about (see order_cancellations)."""
        with self._session_factory() as session:
            result = confirm_cancellation(session, customer_id, request_started_at, now)
            session.commit()
            return result
