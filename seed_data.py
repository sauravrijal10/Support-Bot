"""
seed_data.py

Applies migrations, then inserts demo data: an admin, two customers, a
catalog of product types (digital services, electronics, clothing, books,
home & kitchen, gift cards) with ~30 products, and a few orders and tickets.
Safe to re-run -- it only adds what is missing (and moves the original demo
products out of the catalog-wide "general" type the 0003 migration put them in).

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
from app.models import Customer, Order, Product, ProductType, Ticket  # noqa: E402
from app.schemas.product_type import ProductTypeCreate  # noqa: E402
from app.services.auth import hash_password  # noqa: E402
from app.services.catalog import validate_product, variant_errors  # noqa: E402
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

# Each type's attribute_schema is validated by ProductTypeCreate below, and
# each product's attributes against its type, so bad demo data fails loudly.
DEMO_PRODUCT_TYPES = [
    {
        "slug": "digital-services", "name": "Digital services",
        "description": "Acme Cloud Storage plans and add-ons. Delivered instantly, never out of stock.",
        "attribute_schema": [
            {"key": "storage_gb", "label": "Storage", "type": "integer", "required": True, "unit": "GB"},
            {"key": "devices", "label": "Devices", "type": "text"},
        ],
    },
    {
        "slug": "electronics", "name": "Electronics",
        "description": "Drives, peripherals and audio.",
        "attribute_schema": [
            {"key": "brand", "label": "Brand", "type": "text", "required": True},
            {"key": "capacity", "label": "Capacity", "type": "text", "variant": True},
            {"key": "model", "label": "Model", "type": "text", "variant": True},
            {"key": "color", "label": "Colour", "type": "text", "variant": True},
            {"key": "warranty_months", "label": "Warranty", "type": "integer", "unit": "months"},
            {"key": "wireless", "label": "Wireless", "type": "boolean"},
        ],
    },
    {
        "slug": "clothing", "name": "Clothing",
        "description": "Acme apparel, in several sizes and colours.",
        "attribute_schema": [
            {"key": "size", "label": "Size", "type": "choice", "required": True, "variant": True,
             "options": ["XS", "S", "M", "L", "XL", "XXL", "One size"]},
            {"key": "color", "label": "Colour", "type": "text", "required": True, "variant": True},
            {"key": "material", "label": "Material", "type": "text"},
            {"key": "fit", "label": "Fit", "type": "choice", "options": ["Men", "Women", "Unisex"]},
        ],
    },
    {
        "slug": "books", "name": "Books",
        "description": "Printed books and e-books.",
        "attribute_schema": [
            {"key": "author", "label": "Author", "type": "text", "required": True},
            {"key": "format", "label": "Format", "type": "choice", "required": True, "variant": True,
             "options": ["Paperback", "Hardcover", "E-book"]},
            {"key": "pages", "label": "Pages", "type": "integer"},
            {"key": "publisher", "label": "Publisher", "type": "text"},
        ],
    },
    {
        "slug": "home-kitchen", "name": "Home & kitchen",
        "description": "Drinkware, cookware and kitchen tools.",
        "attribute_schema": [
            {"key": "brand", "label": "Brand", "type": "text", "required": True},
            {"key": "material", "label": "Material", "type": "text"},
            {"key": "capacity_ml", "label": "Capacity", "type": "integer", "unit": "ml"},
            {"key": "dishwasher_safe", "label": "Dishwasher safe", "type": "boolean"},
        ],
    },
    {
        "slug": "gift-cards", "name": "Gift cards",
        "description": "Store credit for anything at Digi Store.",
        "attribute_schema": [
            {"key": "amount", "label": "Amount", "type": "integer", "unit": "USD", "variant": True},
            {"key": "delivery", "label": "Delivery", "type": "choice", "required": True, "variant": True,
             "options": ["Email", "Post"]},
            {"key": "valid_months", "label": "Valid for", "type": "integer", "unit": "months"},
        ],
    },
]


def _p(type_slug, sku, name, description, price, attributes, stock=None, period="one_time",
       active=True, variants=()):
    return {
        "type": type_slug, "sku": sku, "name": name, "description": description,
        "price": Decimal(price), "attributes": attributes, "stock_quantity": stock,
        "billing_period": period, "is_active": active, "variants": list(variants),
    }


def _v(sku, options, price=None, stock=None, active=True):
    """A variant. If a standalone product with this SKU exists from before
    variants (e.g. TSHIRT-LOGO-BLK-M), its stock carries over."""
    return {"sku": sku, "options": options, "price": None if price is None else Decimal(price),
            "stock_quantity": stock, "is_active": active}


# stock=None means unlimited (digital goods); 0 means sold out.
DEMO_PRODUCTS = [
    # --- digital services (the original cloud-storage catalog)
    _p("digital-services", "FREE-PLAN", "Free plan", "5GB storage, 1 device. Includes automatic daily backups and 256-bit encryption.",
       "0.00", {"storage_gb": 5, "devices": "1"}, period="monthly"),
    _p("digital-services", "PLUS-PLAN", "Plus plan", "100GB storage, 3 devices. Includes automatic daily backups and 256-bit encryption.",
       "5.00", {"storage_gb": 100, "devices": "3"}, period="monthly"),
    _p("digital-services", "PRO-PLAN", "Pro plan", "1TB storage, unlimited devices. Includes automatic daily backups and 256-bit encryption.",
       "15.00", {"storage_gb": 1000, "devices": "Unlimited"}, period="monthly"),
    _p("digital-services", "PRO-ANNUAL", "Pro plan - annual", "The Pro plan billed once a year.",
       "150.00", {"storage_gb": 1000, "devices": "Unlimited"}, period="yearly"),
    _p("digital-services", "ADDON-500GB", "Extra 500GB storage add-on", "Adds 500GB of storage to any plan.",
       "25.00", {"storage_gb": 500}),
    # --- electronics
    _p("electronics", "DRIVE-BACKUP", "Acme Backup Drive", "Physical drive for local backups, USB-C. Ships worldwide.",
       "89.00", {"brand": "Acme", "warranty_months": 24}, variants=[
           _v("DRIVE-1TB", {"capacity": "1TB", "model": "ABD-1T", "color": "Black"}, "89.00", stock=10),
           _v("DRIVE-4TB", {"capacity": "4TB", "model": "ABD-4T", "color": "Black"}, "179.00", stock=6),
       ]),
    _p("electronics", "MOUSE-WL", "Silent wireless mouse", "Quiet clicks, 18-month battery life, works with any laptop.",
       "29.99", {"brand": "Nimbus", "model": "SM-200", "warranty_months": 12, "color": "Grey", "wireless": True}, stock=40),
    _p("electronics", "HUB-USBC-7", "USB-C hub 7-in-1", "HDMI 4K, 3x USB-A, SD/microSD, 100W pass-through charging.",
       "49.00", {"brand": "Nimbus", "model": "HB-7", "warranty_months": 12, "color": "Silver", "wireless": False}, stock=0),
    _p("electronics", "HEADPHONES-NC", "Noise-cancelling headphones", "Over-ear, active noise cancelling, 30-hour battery.",
       "199.00", {"brand": "Sonora", "model": "Q30", "warranty_months": 24, "color": "Black", "wireless": True}, stock=12),
    _p("electronics", "KEYBOARD-MECH", "Mechanical keyboard", "Tenkeyless, hot-swappable switches, white backlight.",
       "119.00", {"brand": "Nimbus", "model": "MK-87", "warranty_months": 24, "color": "White", "wireless": False}, stock=15),
    _p("electronics", "MONITOR-27-4K", '27" 4K monitor', "27-inch IPS, 3840x2160, USB-C with 65W charging.",
       "349.00", {"brand": "Vistar", "model": "V27U", "warranty_months": 36, "color": "Black"}, stock=4),
    # --- clothing
    _p("clothing", "TSHIRT-LOGO", "Acme logo T-shirt", "Soft organic cotton tee with the Acme logo.",
       "24.00", {"material": "Organic cotton", "fit": "Unisex"}, variants=[
           _v("TSHIRT-LOGO-BLK-M", {"size": "M", "color": "Black"}, stock=30),
           _v("TSHIRT-LOGO-BLK-L", {"size": "L", "color": "Black"}, stock=25),
           _v("TSHIRT-LOGO-WHT-M", {"size": "M", "color": "White"}, stock=20),
           _v("TSHIRT-LOGO-WHT-L", {"size": "L", "color": "White"}, stock=0),
       ]),
    _p("clothing", "HOODIE-ZIP", "Zip hoodie", "Heavyweight brushed-fleece zip hoodie.",
       "59.00", {"material": "Cotton/polyester fleece", "fit": "Unisex"}, variants=[
           _v("HOODIE-GRY-M", {"size": "M", "color": "Grey"}, stock=10),
           _v("HOODIE-GRY-L", {"size": "L", "color": "Grey"}, stock=12),
           _v("HOODIE-NVY-L", {"size": "L", "color": "Navy"}, "62.00", stock=6),
       ]),
    _p("clothing", "JACKET-RAIN-NVY-M", "Packable rain jacket (navy, M)", "Waterproof, breathable, packs into its own pocket.",
       "89.00", {"size": "M", "color": "Navy", "material": "Recycled nylon", "fit": "Women"}, stock=8),
    _p("clothing", "SOCKS-RUN-3PK", "Running socks, 3-pack", "Cushioned, moisture-wicking running socks.",
       "15.00", {"size": "One size", "color": "White", "material": "Merino blend", "fit": "Unisex"}, stock=50),
    _p("clothing", "CAP-LOGO", "Acme logo cap", "Adjustable six-panel cap with embroidered logo.",
       "19.00", {"size": "One size", "color": "Navy", "material": "Cotton twill", "fit": "Unisex"}, stock=35),
    # --- books (fictional titles)
    _p("books", "BOOK-BACKUP-HANDBOOK", "The Backup Handbook", "A practical guide to backing up everything that matters.",
       "34.99", {"author": "Maya Rivera", "pages": 312, "publisher": "Acme Press"}, variants=[
           _v("BOOK-BACKUP-HANDBOOK-PB", {"format": "Paperback"}, "34.99", stock=20),
           _v("BOOK-BACKUP-HANDBOOK-EBOOK", {"format": "E-book"}, "14.99"),
       ]),
    _p("books", "BOOK-CLOUD-BASICS", "Cloud Storage Basics", "A short, friendly introduction to how cloud storage works.",
       "12.99", {"author": "Priya Nair", "format": "E-book", "pages": 180, "publisher": "Acme Press"}),
    _p("books", "BOOK-QUIET-SERVERS", "The Quiet Servers", "A novel about a night-shift engineer and a data centre that keeps secrets.",
       "27.50", {"author": "Tom Ashby", "format": "Hardcover", "pages": 420}, stock=9),
    _p("books", "BOOK-KITCHEN-DATA", "Kitchen Table Data", "Organising family photos, documents and passwords, one weekend at a time.",
       "22.00", {"author": "Lena Okafor", "format": "Paperback", "pages": 256}, stock=14),
    # --- home & kitchen
    _p("home-kitchen", "BOTTLE-750", "Insulated water bottle 750ml", "Keeps drinks cold for 24 hours or hot for 12.",
       "32.00", {"brand": "Acme", "material": "Stainless steel", "capacity_ml": 750, "dishwasher_safe": False}, stock=45),
    _p("home-kitchen", "MUG-CERAMIC", "Ceramic coffee mug", "Stoneware mug with a matte glaze.",
       "14.00", {"brand": "Acme", "material": "Stoneware", "capacity_ml": 350, "dishwasher_safe": True}, stock=60),
    _p("home-kitchen", "KNIFE-CHEF-20", "Chef's knife 20cm", "Forged German steel with a full tang.",
       "79.00", {"brand": "Hearth", "material": "Stainless steel", "dishwasher_safe": False}, stock=10),
    _p("home-kitchen", "BOARD-BAMBOO", "Bamboo cutting board", "Large reversible board with a juice groove.",
       "29.00", {"brand": "Hearth", "material": "Bamboo", "dishwasher_safe": False}, stock=18),
    _p("home-kitchen", "KETTLE-CLASSIC", "Classic stovetop kettle", "Discontinued -- kept for order history.",
       "45.00", {"brand": "Hearth", "material": "Enamelled steel", "capacity_ml": 1700}, stock=0, active=False),
    # --- gift cards
    _p("gift-cards", "GIFT-CARD", "Digi Store gift card",
       "E-gift cards arrive by email within minutes; posted cards come in a gift envelope.",
       "25.00", {"valid_months": 24}, variants=[
           _v("GIFT-25-EMAIL", {"amount": 25, "delivery": "Email"}, "25.00"),
           _v("GIFT-50-EMAIL", {"amount": 50, "delivery": "Email"}, "50.00"),
           _v("GIFT-100-POST", {"amount": 100, "delivery": "Post"}, "100.00", stock=100),
       ]),
]

# The type the 0003 migration put pre-existing products in.
FALLBACK_TYPE = "general"


def _fields(spec: dict, product_type: ProductType, variants: list[dict]) -> dict:
    """Product columns from a spec, attributes/variants validated against the type."""
    fields = {k: v for k, v in spec.items() if k not in ("type", "variants")}
    fields["attributes"], fields["variants"] = validate_product(
        product_type.attribute_schema, spec["attributes"], variants, spec["sku"], product_type.name
    )
    if fields["variants"]:
        fields["stock_quantity"] = None  # stock is per variant
    return fields


def _absorb_standalone_products(session, variants: list[dict], carry: dict | None = None) -> dict:
    """Before variants, each size/format was its own product (e.g. SKU
    TSHIRT-LOGO-BLK-M). For each variant whose SKU such a product still
    holds: carry its stock over, then rename and deactivate it (it stays for
    the order history that points at it)."""
    carry = dict(carry or {})
    absorbed = 0
    for v in variants:
        old = session.scalar(select(Product).where(Product.sku == v["sku"], Product.variants == []))
        if old is None:
            continue
        carry.setdefault(v["sku"], old.stock_quantity)
        old.sku = f"{old.sku}-OLD{old.id}"[:64]
        old.is_active = False
        absorbed += 1
    session.flush()
    out = [{**v, "stock_quantity": carry[v["sku"]]} if v["sku"] in carry else v for v in variants]
    return {"list": out, "absorbed": absorbed}


def _upgrade_type_for_variants(session, product_type: ProductType, schema: list[dict]) -> bool:
    """A demo type created before variants existed gets the new schema (with
    "variant" flags), if every product it has would still be valid."""
    if any(a.get("variant") for a in product_type.attribute_schema) or not any(
        a.get("variant") for a in schema
    ):
        return False
    products = session.scalars(select(Product).where(Product.product_type_id == product_type.id))
    if any(variant_errors(schema, p.attributes, p.variants, p.sku) for p in products):
        return False
    product_type.attribute_schema = schema
    return True


def new_password() -> str:
    return secrets.token_urlsafe(9)


def main():
    run_migrations()
    engine = create_engine(sqlalchemy_url(os.environ["DATABASE_URL"]))
    issued: dict[str, str] = {}
    warnings: list[str] = []
    counts = {"types": 0, "types_upgraded": 0, "products": 0, "recategorised": 0, "converted": 0}

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

        # --- product types --------------------------------------------------
        types: dict[str, ProductType] = {}
        for spec in DEMO_PRODUCT_TYPES:
            definition = ProductTypeCreate.model_validate(spec)
            schema = [a.model_dump(exclude_none=True) for a in definition.attribute_schema]
            product_type = session.scalar(select(ProductType).where(ProductType.slug == definition.slug))
            if product_type is None:
                product_type = ProductType(**{**definition.model_dump(), "attribute_schema": schema})
                session.add(product_type)
                counts["types"] += 1
            elif _upgrade_type_for_variants(session, product_type, schema):
                counts["types_upgraded"] += 1
            # Otherwise an existing type is left alone: an admin may have edited it.
            types[definition.slug] = product_type
        session.flush()

        # --- products -----------------------------------------------------
        for spec in DEMO_PRODUCTS:
            product_type = types[spec["type"]]
            product = session.scalar(select(Product).where(Product.sku == spec["sku"]))
            if product is None:
                variants = _absorb_standalone_products(session, spec["variants"])
                counts["converted"] += variants["absorbed"]
                session.add(Product(**_fields(spec, product_type, variants["list"]), product_type=product_type))
                counts["products"] += 1
            elif product.product_type.slug == FALLBACK_TYPE and not spec["variants"]:
                product.product_type = product_type
                product.attributes = _fields(spec, product_type, [])["attributes"]
                counts["recategorised"] += 1
            elif spec["variants"] and not product.variants:
                # Same SKU, now with variants (e.g. a book gaining an e-book
                # edition): the product's own stock goes to its first variant.
                variants = _absorb_standalone_products(
                    session, spec["variants"], carry={spec["variants"][0]["sku"]: product.stock_quantity}
                )
                fields = _fields(spec, product_type, variants["list"])
                product.product_type = product_type
                product.attributes, product.variants = fields["attributes"], fields["variants"]
                product.stock_quantity = None
                counts["converted"] += 1 + variants["absorbed"]
        session.flush()

        # --- orders and tickets (only on an empty database) ---------------
        if session.scalar(select(Order.id).limit(1)) is None:
            products = {p.sku: p for p in session.scalars(select(Product))}
            ada_order = create_order(session, customers[ADA].id, [(products["PRO-ANNUAL"].id, None, 1)])
            ada_order.status = "completed"
            grace_order = create_order(session, customers[GRACE].id, [(products["ADDON-500GB"].id, None, 1)])
            grace_order.status = "processing"
            create_order(
                session, customers[ADA].id,
                [(products["BOOK-BACKUP-HANDBOOK"].id, "BOOK-BACKUP-HANDBOOK-PB", 1),
                 (products["TSHIRT-LOGO"].id, "TSHIRT-LOGO-BLK-M", 2)],
            )
            headphones = create_order(session, customers[GRACE].id, [(products["HEADPHONES-NC"].id, None, 1)])
            headphones.status = "completed"

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

    print(
        f"Demo data ready: {counts['types']} new product type(s) ({counts['types_upgraded']} "
        f"upgraded for variants), {counts['products']} new product(s), {counts['recategorised']} "
        f"moved to their real type, {counts['converted']} turned into variants."
    )
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
