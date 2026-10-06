from decimal import Decimal

import sqlalchemy as sa

from fastapi import Depends, HTTPException, Query, Response, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_optional_customer, get_session, require_admin
from app.api.views.base import APIView, route
from app.models import Product, ProductType
from app.schemas.product import ProductCreate, ProductOut, ProductUpdate
from app.services.catalog import validate_product
from app.services.variants import in_stock_clause, lock_skus, sku_taken

UNIQUE_VIOLATION = "23505"
FOREIGN_KEY_VIOLATION = "23503"


def _is_admin(customer: dict | None) -> bool:
    return bool(customer and customer["is_admin"])


def _pgcode(error: IntegrityError) -> str | None:
    return getattr(error.orig, "pgcode", None)


def _type_for_validation(session: Session, type_id: int, *, new_assignment: bool) -> ProductType:
    """The type to validate a product's attributes against, share-locked so
    an admin can't change its attribute schema mid-validation. A product can
    only be *moved into* an active type; existing products of an inactive
    type can still be edited."""
    product_type = session.get(ProductType, type_id, with_for_update={"read": True})
    if product_type is None or (new_assignment and not product_type.is_active):
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY, f"Unknown or inactive product type: {type_id}"
        )
    return product_type


def _validated(
    session: Session, product_type: ProductType, sku: str, attributes: dict, variants: list[dict],
    stock_quantity: int | None, exclude_product_id: int | None = None,
) -> tuple[dict, list[dict]]:
    """Check attributes + variants against the type, and that no SKU (the
    product's or a variant's) is used by another product or its variants.
    Returns (attributes, variants) as stored."""
    attributes, variants = validate_product(
        product_type.attribute_schema, attributes, variants, sku, product_type.name
    )
    if variants and stock_quantity is not None:
        raise HTTPException(
            status.HTTP_422_UNPROCESSABLE_ENTITY,
            "A product with variants keeps stock per variant: set stock_quantity to null.",
        )
    skus = [sku, *(v["sku"] for v in variants)]
    lock_skus(session, skus)  # held to commit, so two writers can't claim the same SKU
    taken = [s for s in skus if sku_taken(session, s, exclude_product_id)]
    if taken:
        raise HTTPException(status.HTTP_409_CONFLICT, f"SKU(s) already used by another product: {taken}")
    return attributes, variants


class ProductView(APIView):
    """The product catalog. Anyone can read active products; only admins can
    see inactive ones or make changes."""

    prefix = "/products"
    tags = ["products"]

    @route("GET", "", response_model=list[ProductOut])
    def list_products(
        self,
        q: str | None = Query(None, max_length=100, description="Search name, description or SKU"),
        product_type: str | None = Query(
            None, alias="type", max_length=64, description="Product type slug, e.g. 'books'"
        ),
        min_price: Decimal | None = Query(None, ge=0),
        max_price: Decimal | None = Query(None, ge=0),
        in_stock: bool = Query(False, description="Only products that can be bought now"),
        include_inactive: bool = Query(False, description="Admins only"),
        limit: int = Query(20, ge=1, le=100),
        offset: int = Query(0, ge=0),
        session: Session = Depends(get_session),
        customer: dict | None = Depends(get_optional_customer),
    ):
        if include_inactive and not _is_admin(customer):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only admins can list inactive products")

        stmt = select(Product)
        if not include_inactive:
            stmt = stmt.where(Product.is_active.is_(True))
        if q:
            stmt = stmt.where(
                or_(
                    Product.name.icontains(q, autoescape=True),
                    Product.description.icontains(q, autoescape=True),
                    Product.sku.icontains(q, autoescape=True),
                    sa.cast(Product.variants, sa.Text).icontains(q, autoescape=True),
                )
            )
        if product_type:
            stmt = stmt.join(Product.product_type).where(ProductType.slug == product_type)
        if min_price is not None:
            stmt = stmt.where(Product.price >= min_price)
        if max_price is not None:
            stmt = stmt.where(Product.price <= max_price)
        if in_stock:
            stmt = stmt.where(in_stock_clause())
        stmt = stmt.order_by(Product.name, Product.id).limit(limit).offset(offset)
        return session.scalars(stmt).all()

    @route("GET", "/{product_id}", response_model=ProductOut)
    def get_product(
        self,
        product_id: int,
        session: Session = Depends(get_session),
        customer: dict | None = Depends(get_optional_customer),
    ):
        product = session.get(Product, product_id)
        # Inactive products look nonexistent to everyone except admins.
        if product is None or (not product.is_active and not _is_admin(customer)):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
        return product

    @route("POST", "", response_model=ProductOut, status_code=status.HTTP_201_CREATED)
    def create_product(
        self,
        payload: ProductCreate,
        session: Session = Depends(get_session),
        admin: dict = Depends(require_admin),
    ):
        product_type = _type_for_validation(session, payload.product_type_id, new_assignment=True)
        data = payload.model_dump()
        data["attributes"], data["variants"] = _validated(
            session, product_type, payload.sku, payload.attributes,
            [v.model_dump() for v in payload.variants], payload.stock_quantity,
        )
        product = Product(**data, product_type=product_type)
        session.add(product)
        try:
            session.commit()
        except IntegrityError as error:
            session.rollback()
            if _pgcode(error) == UNIQUE_VIOLATION:
                raise HTTPException(status.HTTP_409_CONFLICT, "A product with this SKU already exists")
            raise
        return product

    @route("PATCH", "/{product_id}", response_model=ProductOut)
    def update_product(
        self,
        product_id: int,
        payload: ProductUpdate,
        session: Session = Depends(get_session),
        admin: dict = Depends(require_admin),
    ):
        product = session.get(Product, product_id)
        if product is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")

        changes = payload.model_dump(exclude_unset=True)
        if changes.keys() & {"product_type_id", "attributes", "variants", "sku", "stock_quantity"}:
            type_id = changes.get("product_type_id", product.product_type_id)
            product_type = _type_for_validation(
                session, type_id, new_assignment=type_id != product.product_type_id
            )
            changes["attributes"], changes["variants"] = _validated(
                session, product_type,
                changes.get("sku", product.sku),
                changes.get("attributes", product.attributes),
                changes.get("variants", product.variants),
                changes.get("stock_quantity", product.stock_quantity),
                exclude_product_id=product.id,
            )
            product.product_type = product_type

        for field, value in changes.items():
            setattr(product, field, value)
        try:
            session.commit()
        except IntegrityError as error:
            session.rollback()
            if _pgcode(error) == UNIQUE_VIOLATION:
                raise HTTPException(status.HTTP_409_CONFLICT, "A product with this SKU already exists")
            raise
        return product

    @route("DELETE", "/{product_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_product(
        self,
        product_id: int,
        session: Session = Depends(get_session),
        admin: dict = Depends(require_admin),
    ):
        product = session.get(Product, product_id)
        if product is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Product not found")
        session.delete(product)
        try:
            session.commit()
        except IntegrityError as error:
            session.rollback()
            if _pgcode(error) == FOREIGN_KEY_VIOLATION:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "This product appears on existing orders and can't be deleted. "
                    "Deactivate it instead (PATCH is_active=false).",
                )
            raise
        return Response(status_code=status.HTTP_204_NO_CONTENT)
