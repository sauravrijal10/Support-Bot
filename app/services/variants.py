"""
app/services/variants.py

Product variants live in the product's `variants` JSONB column:

    [{"sku": "TSHIRT-LOGO-BLK-M", "options": {"size": "M", "color": "Black"},
      "price": "24.00" | null,          # null = the product's price
      "stock_quantity": 30 | null,      # null = unlimited
      "is_active": true}]

A product with no variants is sold as itself (product.price / stock). These
helpers hide that difference: everything that sells, prices or stocks an item
works on a (product, variant-or-None) pair.

Because variants aren't rows, Postgres can't enforce SKU uniqueness across
them; lock_skus + sku_taken do it in the app, under advisory locks so two
concurrent writes can't both claim one SKU. Stock changes happen while the
product row is locked (FOR UPDATE), and replace the JSON list rather than
mutate it, so SQLAlchemy notices the change.
"""

from decimal import Decimal

from sqlalchemy import and_, func, literal_column, or_, select, text
from sqlalchemy.orm import Session

from app.models import Product
from app.services.catalog import describe_attributes, split_schema


def find_variant(product: Product, sku: str | None) -> dict | None:
    if not sku:
        return None
    sku = sku.upper()
    return next((v for v in product.variants or [] if v["sku"] == sku), None)


def is_sellable(product: Product, variant: dict | None) -> bool:
    if not product.is_active:
        return False
    if product.variants:
        return variant is not None and variant.get("is_active", True)
    return variant is None


def unit_price(product: Product, variant: dict | None) -> Decimal:
    if variant is not None and variant.get("price") is not None:
        return Decimal(variant["price"])
    return product.price


def stock_of(product: Product, variant: dict | None) -> int | None:
    return variant.get("stock_quantity") if variant is not None else product.stock_quantity


def label(product: Product, variant: dict | None) -> str:
    """"Size: M, Colour: Black" -- or "" for a product without variants."""
    if variant is None:
        return ""
    _, variant_defs = split_schema(product.product_type.attribute_schema)
    shown = describe_attributes(variant_defs, variant.get("options"))
    return ", ".join(f"{k}: {v}" for k, v in shown.items())


def display_name(product: Product, variant: dict | None) -> str:
    text_ = label(product, variant)
    return f"{product.name} ({text_})" if text_ else product.name


def change_stock(product: Product, variant: dict | None, delta: int) -> None:
    """Add `delta` (negative to take) to the item's stock; unlimited stays
    unlimited. The caller holds the product row lock."""
    if variant is None:
        if product.stock_quantity is not None:
            product.stock_quantity += delta
        return
    if variant.get("stock_quantity") is None:
        return
    product.variants = [
        {**v, "stock_quantity": v["stock_quantity"] + delta} if v["sku"] == variant["sku"] else v
        for v in product.variants
    ]


def sellable_items(product: Product) -> list[tuple[Product, dict | None]]:
    """What a customer can pick for this product: its active variants, or the
    product itself."""
    if product.variants:
        return [(product, v) for v in product.variants if v.get("is_active", True)]
    return [(product, None)]


# Any active variant with stock left (or unlimited stock).
_VARIANT_IN_STOCK = (
    '$[*] ? (@.is_active == true && (@.stock_quantity == null || @.stock_quantity > 0))'
)


def in_stock_clause():
    """SQL condition: the product can be bought now -- its own stock for a
    product without variants, otherwise any active variant in stock."""
    no_variants = func.jsonb_array_length(Product.variants) == 0
    return or_(
        and_(no_variants, or_(Product.stock_quantity.is_(None), Product.stock_quantity > 0)),
        and_(~no_variants, func.jsonb_path_exists(
            Product.variants, literal_column(f"'{_VARIANT_IN_STOCK}'::jsonpath")
        )),
    )


def _owner_filter(sku: str):
    return or_(func.upper(Product.sku) == sku, Product.variants.contains([{"sku": sku}]))


def resolve_sku(session: Session, sku: str, *, lock: bool = False):
    """(product, variant-or-None) for a product or variant SKU, or None.
    Inactive products/variants are returned too; check is_sellable."""
    sku = sku.strip().upper()
    stmt = select(Product).where(_owner_filter(sku))
    if lock:
        stmt = stmt.with_for_update(of=Product)
    product = session.scalars(stmt).first()
    if product is None:
        return None
    if product.sku.upper() == sku:
        return product, None
    return product, find_variant(product, sku)


def lock_skus(session: Session, skus: list[str]) -> None:
    """Transaction-scoped advisory locks on these SKUs (sorted, so two
    writers can't deadlock), held until commit/rollback."""
    for sku in sorted({s.upper() for s in skus}):
        session.execute(text("SELECT pg_advisory_xact_lock(hashtext(:k))"), {"k": "sku:" + sku})


def sku_taken(session: Session, sku: str, exclude_product_id: int | None = None) -> bool:
    """Is this SKU used by another product, or by another product's variant?"""
    stmt = select(Product.id).where(_owner_filter(sku.upper()))
    if exclude_product_id is not None:
        stmt = stmt.where(Product.id != exclude_product_id)
    return session.scalar(stmt.limit(1)) is not None
