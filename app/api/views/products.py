from fastapi import Depends, HTTPException, Query, Response, status
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_optional_customer, get_session, require_admin
from app.api.views.base import APIView, route
from app.models import Product
from app.schemas.product import ProductCreate, ProductOut, ProductUpdate

UNIQUE_VIOLATION = "23505"
FOREIGN_KEY_VIOLATION = "23503"


def _is_admin(customer: dict | None) -> bool:
    return bool(customer and customer["is_admin"])


def _pgcode(error: IntegrityError) -> str | None:
    return getattr(error.orig, "pgcode", None)


class ProductView(APIView):
    """The product catalog. Anyone can read active products; only admins can
    see inactive ones or make changes."""

    prefix = "/products"
    tags = ["products"]

    @route("GET", "", response_model=list[ProductOut])
    def list_products(
        self,
        q: str | None = Query(None, max_length=100, description="Search name, description or SKU"),
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
                )
            )
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
        product = Product(**payload.model_dump())
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

        for field, value in payload.model_dump(exclude_unset=True).items():
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
