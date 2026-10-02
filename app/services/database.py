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

from sqlalchemy import or_, select
from sqlalchemy.dialects.postgresql import insert as pg_insert
from sqlalchemy.orm import sessionmaker

from app.models import Customer, Order, Product, Ticket

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

    def search_products(self, terms, max_price=None, in_stock_only=False, limit=10):
        """Active products only (public store data). `terms` are already
        cleaned keywords; a product matches if ANY term appears in its name,
        description or SKU. No terms = every active product.
        Returns (rows, total_matches), cheapest first."""
        with self._session_factory() as session:
            stmt = select(Product).where(Product.is_active.is_(True))
            if terms:
                stmt = stmt.where(
                    or_(
                        *[
                            or_(
                                Product.name.icontains(t, autoescape=True),
                                Product.description.icontains(t, autoescape=True),
                                Product.sku.icontains(t, autoescape=True),
                            )
                            for t in terms
                        ]
                    )
                )
            if max_price is not None:
                stmt = stmt.where(Product.price <= max_price)
            if in_stock_only:
                stmt = stmt.where(
                    or_(Product.stock_quantity.is_(None), Product.stock_quantity > 0)
                )

            total = len(session.scalars(stmt.with_only_columns(Product.id)).all())
            products = session.scalars(
                stmt.order_by(Product.price, Product.name, Product.id).limit(limit)
            ).all()
            rows = [
                {
                    "sku": p.sku,
                    "name": p.name,
                    "description": p.description,
                    "price": p.price,
                    "billing_period": p.billing_period,
                    "stock_quantity": p.stock_quantity,
                }
                for p in products
            ]
            return rows, total
