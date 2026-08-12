from fastapi import APIRouter
from pydantic import BaseModel

from scout.db.session import SessionLocal
from scout.repositories.product_repository import ProductRepository
from scout.services.product_service import check_stock, _get_active_promotion_dict

router = APIRouter()


class AddToCartRequest(BaseModel):
    product_id: str
    quantity: int = 1
    size: str | None = None
    color: str | None = None


def _variant_label(product_name: str, color: str | None, size: str | None) -> str:
    details = []
    if color:
        details.append(color.title())
    if size:
        details.append(f"size {size}")
    return f"{product_name} in {', '.join(details)}" if details else product_name


@router.post("/cart/add")
def add_to_cart(request: AddToCartRequest):
    """Authoritative add-to-cart validation endpoint. The frontend never
    sends a price — only a product_id and quantity. This looks up the
    real product, checks real stock, and returns the true price (including
    any active promotion) so the client-side cart never has to trust a
    browser-supplied value for what something costs or whether it's
    actually available.
    """
    session = SessionLocal()
    try:
        product_repo = ProductRepository(session)
        product = product_repo.get_by_id(request.product_id)

        if not product:
            return {"success": False, "error": "Product not found"}

        if request.quantity < 1:
            return {"success": False, "error": "Quantity must be at least 1"}

        stock_info = check_stock(
            session,
            request.product_id,
            size=request.size,
            color=request.color,
        )
        requested_variant = bool(request.size or request.color)
        if not stock_info["in_stock"]:
            error = "Out of stock"
            if requested_variant:
                error = f"{_variant_label(product.name, stock_info['requested_color'], stock_info['requested_size'])} is currently out of stock."
            return {
                "success": False,
                "error": error,
                "in_stock": False,
            }

        if request.quantity > stock_info["total_quantity"]:
            return {
                "success": False,
                "error": (
                    f"Only {stock_info['total_quantity']} available for "
                    f"{_variant_label(product.name, stock_info['requested_color'], stock_info['requested_size'])}."
                ),
                "in_stock": True,
                "available_quantity": stock_info["total_quantity"],
            }

        promotion = _get_active_promotion_dict(session, product)
        unit_price = promotion["discounted_price"] if promotion else product.price

        return {
            "success": True,
            "in_stock": True,
            "product_id": product.product_id,
            "name": product.name,
            "brand": product.brand,
            "image_url": product.image_url,
            "unit_price": unit_price,
            "quantity": request.quantity,
            "line_total": round(unit_price * request.quantity, 2),
            "promotion": promotion,
            "size": stock_info["requested_size"],
            "color": stock_info["requested_color"],
            "available_quantity": stock_info["total_quantity"],
        }
    finally:
        session.close()
