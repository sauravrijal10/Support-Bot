from fastapi import Depends, HTTPException, Query, Response, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.api.deps import get_optional_customer, get_session, require_admin
from app.api.views.base import APIView, route
from app.api.views.products import FOREIGN_KEY_VIOLATION, UNIQUE_VIOLATION, _is_admin, _pgcode
from app.models import Product, ProductType
from app.schemas.product_type import ProductTypeCreate, ProductTypeOut, ProductTypeUpdate
from app.services.catalog import variant_errors

DUPLICATE_SLUG = "A product type with this slug already exists"


class ProductTypeView(APIView):
    """Product categories and the attributes their products carry. Anyone can
    read active types; only admins can see inactive ones or make changes."""

    prefix = "/product-types"
    tags = ["product types"]

    @route("GET", "", response_model=list[ProductTypeOut])
    def list_types(
        self,
        include_inactive: bool = Query(False, description="Admins only"),
        session: Session = Depends(get_session),
        customer: dict | None = Depends(get_optional_customer),
    ):
        if include_inactive and not _is_admin(customer):
            raise HTTPException(status.HTTP_403_FORBIDDEN, "Only admins can list inactive types")
        stmt = select(ProductType)
        if not include_inactive:
            stmt = stmt.where(ProductType.is_active.is_(True))
        return session.scalars(stmt.order_by(ProductType.name, ProductType.id)).all()

    @route("GET", "/{type_id}", response_model=ProductTypeOut)
    def get_type(
        self,
        type_id: int,
        session: Session = Depends(get_session),
        customer: dict | None = Depends(get_optional_customer),
    ):
        product_type = session.get(ProductType, type_id)
        if product_type is None or (not product_type.is_active and not _is_admin(customer)):
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Product type not found")
        return product_type

    @route("POST", "", response_model=ProductTypeOut, status_code=status.HTTP_201_CREATED)
    def create_type(
        self,
        payload: ProductTypeCreate,
        session: Session = Depends(get_session),
        admin: dict = Depends(require_admin),
    ):
        data = payload.model_dump()
        data["attribute_schema"] = [a.model_dump(exclude_none=True) for a in payload.attribute_schema]
        product_type = ProductType(**data)
        session.add(product_type)
        self._commit(session)
        return product_type

    @route("PATCH", "/{type_id}", response_model=ProductTypeOut)
    def update_type(
        self,
        type_id: int,
        payload: ProductTypeUpdate,
        session: Session = Depends(get_session),
        admin: dict = Depends(require_admin),
    ):
        # Exclusive lock: product writes hold a shared lock on their type
        # while validating, so none can slip in under the old schema.
        product_type = session.get(ProductType, type_id, with_for_update=True)
        if product_type is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Product type not found")

        changes = payload.model_dump(exclude_unset=True)
        if "attribute_schema" in changes:
            schema = [a.model_dump(exclude_none=True) for a in payload.attribute_schema]
            # Refuse a schema that existing products would break, rather than
            # leave the catalog holding products that fail their own type.
            products = session.scalars(
                select(Product).where(Product.product_type_id == type_id).order_by(Product.id)
            ).all()
            broken = [(p.sku, variant_errors(schema, p.attributes, p.variants, p.sku)) for p in products]
            broken = [(sku, errors) for sku, errors in broken if errors]
            if broken:
                sku, errors = broken[0]
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    f"{len(broken)} existing product(s) would not match the new attributes "
                    f"(e.g. {sku}: {'; '.join(errors)}). Update those products first.",
                )
            changes["attribute_schema"] = schema

        for field, value in changes.items():
            setattr(product_type, field, value)
        self._commit(session)
        return product_type

    @route("DELETE", "/{type_id}", status_code=status.HTTP_204_NO_CONTENT)
    def delete_type(
        self,
        type_id: int,
        session: Session = Depends(get_session),
        admin: dict = Depends(require_admin),
    ):
        product_type = session.get(ProductType, type_id)
        if product_type is None:
            raise HTTPException(status.HTTP_404_NOT_FOUND, "Product type not found")
        session.delete(product_type)
        try:
            session.commit()
        except IntegrityError as error:
            session.rollback()
            if _pgcode(error) == FOREIGN_KEY_VIOLATION:
                raise HTTPException(
                    status.HTTP_409_CONFLICT,
                    "This type still has products and can't be deleted. Move them to another "
                    "type, or deactivate the type instead (PATCH is_active=false).",
                )
            raise
        return Response(status_code=status.HTTP_204_NO_CONTENT)

    @staticmethod
    def _commit(session: Session) -> None:
        try:
            session.commit()
        except IntegrityError as error:
            session.rollback()
            if _pgcode(error) == UNIQUE_VIOLATION:
                raise HTTPException(status.HTTP_409_CONFLICT, DUPLICATE_SLUG)
            raise
