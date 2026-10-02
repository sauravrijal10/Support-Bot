"""
seed_data.py

Applies migrations, then inserts demo data: an admin, two customers, a small
product catalog, and a few orders and tickets. Safe to re-run -- it only adds
what is missing.

Every account it creates gets a RANDOM password, printed once below -- there
are no default credentials. An account that already exists is left alone
(a password is only assigned to a demo account that has none). To get fresh
logins, recreate the database (`docker compose down -v`) and seed again.

  python seed_data.py

Demo data only: don't run this against a production database.
"""

import os
import secrets
from decimal import Decimal

from dotenv import load_dotenv
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session

load_dotenv()

from app.db.migrate import run_migrations  # noqa: E402
from app.db.session import sqlalchemy_url  # noqa: E402
from app.models import Customer, Order, Product, Ticket  # noqa: E402
from app.services.auth import hash_password  # noqa: E402
from app.services.orders import create_order  # noqa: E402

ADA = "ada@example.com"
GRACE = "grace@example.com"
ADMIN = "admin@example.com"

DEMO_CUSTOMERS = [
    # (name, email, plan, is_admin)
    ("Support Admin", ADMIN, "free", True),
    ("Ada Lovelace", ADA, "pro", False),
    ("Grace Hopper", GRACE, "plus", False),
]

# (sku, name, description, price, billing_period, stock_quantity)
DEMO_PRODUCTS = [
    ("FREE-PLAN", "Free plan", "5GB storage, 1 device. Includes automatic daily backups and 256-bit encryption.", "0.00", "monthly", None),
    ("PLUS-PLAN", "Plus plan", "100GB storage, 3 devices. Includes automatic daily backups and 256-bit encryption.", "5.00", "monthly", None),
    ("PRO-PLAN", "Pro plan", "1TB storage, unlimited devices. Includes automatic daily backups and 256-bit encryption.", "15.00", "monthly", None),
    ("PRO-ANNUAL", "Pro plan - annual", "The Pro plan billed once a year.", "150.00", "yearly", None),
    ("ADDON-500GB", "Extra 500GB storage add-on", "Adds 500GB of storage to any plan.", "25.00", "one_time", None),
    ("DRIVE-1TB", "Acme Backup Drive 1TB", "Physical 1TB drive for local backups. Ships worldwide.", "89.00", "one_time", 10),
]


def new_password() -> str:
    return secrets.token_urlsafe(9)


def main():
    run_migrations()
    engine = create_engine(sqlalchemy_url(os.environ["DATABASE_URL"]))
    issued: dict[str, str] = {}
    warnings: list[str] = []

    with Session(engine) as session:
        # --- customers ----------------------------------------------------
        customers: dict[str, Customer] = {}
        for name, email, plan, is_admin in DEMO_CUSTOMERS:
            customer = session.scalar(select(Customer).where(Customer.email == email))
            if customer is None:
                password = new_password()
                customer = Customer(
                    name=name, email=email, plan=plan, is_admin=is_admin,
                    password_hash=hash_password(password),
                )
                session.add(customer)
                issued[email] = password
            else:
                if customer.password_hash is None:
                    password = new_password()
                    customer.password_hash = hash_password(password)
                    issued[email] = password
                # Never promote a pre-existing account: someone could have
                # registered admin@example.com before this script ran.
                if is_admin and not customer.is_admin:
                    warnings.append(
                        f"{email} already exists and is NOT an admin; not promoting it. "
                        "Recreate the database or promote it manually if you own it."
                    )
            customers[email] = customer
        session.flush()

        # --- products -----------------------------------------------------
        for sku, name, description, price, period, stock in DEMO_PRODUCTS:
            if session.scalar(select(Product).where(Product.sku == sku)) is None:
                session.add(
                    Product(
                        sku=sku, name=name, description=description,
                        price=Decimal(price), billing_period=period, stock_quantity=stock,
                    )
                )
        session.flush()

        # --- orders and tickets (only on an empty database) ---------------
        if session.scalar(select(Order.id).limit(1)) is None:
            products = {p.sku: p for p in session.scalars(select(Product))}
            ada_order = create_order(session, customers[ADA].id, [(products["PRO-ANNUAL"].id, 1)])
            ada_order.status = "completed"
            grace_order = create_order(session, customers[GRACE].id, [(products["ADDON-500GB"].id, 1)])
            grace_order.status = "processing"

        if session.scalar(select(Ticket.id).limit(1)) is None:
            session.add_all(
                [
                    Ticket(customer_id=customers[ADA].id, subject="Cannot access shared folder",
                           description="I shared a folder with a colleague but they get a permissions error.",
                           status="open"),
                    Ticket(customer_id=customers[GRACE].id, subject="Billing question about renewal",
                           description="Will my plan renew at the same price?", status="resolved"),
                ]
            )

        session.commit()

    print("Demo data ready.")
    if issued:
        print("\nNew demo logins (shown once -- these are not stored in plain text):")
        for email, password in issued.items():
            print(f"  {email}  /  {password}")
    else:
        print("All demo accounts already have passwords; left unchanged.")
    for warning in warnings:
        print(f"\nWARNING: {warning}")


if __name__ == "__main__":
    main()
