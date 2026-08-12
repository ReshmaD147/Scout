from scout.api.cart import AddToCartRequest, add_to_cart
from scout.api.checkout import CheckoutItem, CheckoutRequest, checkout
from scout.api.products import get_product_detail, get_product_stock
from scout.db.session import SessionLocal
from scout.services.product_service import check_stock, find_alternatives
from scout.services.store_service import check_store_stock, get_fulfillment_options


def test_black_small_available_add_to_cart_succeeds():
    result = add_to_cart(
        AddToCartRequest(product_id="P001", quantity=1, color="black", size="S")
    )

    assert result["success"] is True
    assert result["in_stock"] is True
    assert result["color"] == "black"
    assert result["size"] == "S"
    assert result["available_quantity"] == 3


def test_black_medium_out_of_stock_is_reported_by_product_stock():
    result = get_product_stock("P001", color="black", size="M")

    assert result["found"] is True
    assert result["in_stock"] is False
    assert result["requested_color"] == "black"
    assert result["requested_size"] == "M"
    assert result["total_quantity"] == 0


def test_black_medium_out_of_stock_is_not_returned_as_nearby_store_stock():
    session = SessionLocal()
    try:
        result = check_store_stock(session, "P001", size="M", color="black")
    finally:
        session.close()

    assert result["found"] is True
    assert result["variant_available"] is False
    assert result["requested_color"] == "black"
    assert result["requested_size"] == "M"
    assert result["stores"] == []


def test_black_medium_out_of_stock_is_not_available_for_delivery():
    session = SessionLocal()
    try:
        result = get_fulfillment_options(session, "P001", size="M", color="black")
    finally:
        session.close()

    assert result["found"] is True
    assert result["requested_color"] == "black"
    assert result["requested_size"] == "M"
    assert result["delivery"]["available"] is False
    assert result["delivery"]["quantity"] == 0
    assert "standard_estimate" not in result["delivery"]


def test_available_variant_is_available_for_delivery_with_verified_quantity():
    session = SessionLocal()
    try:
        result = get_fulfillment_options(session, "P001", size="S", color="black")
    finally:
        session.close()

    assert result["found"] is True
    assert result["delivery"]["available"] is True
    assert result["delivery"]["quantity"] == 3
    assert result["delivery"]["standard_estimate"] == "3-5 business days"


def test_similar_products_preserve_variant_budget_and_exclude_original():
    session = SessionLocal()
    try:
        results = find_alternatives(
            session,
            "P001",
            size="M",
            color="floral",
            max_price=80,
            category="dresses",
            limit=3,
        )
        product_ids = [product["product_id"] for product in results]
        stock_results = [
            check_stock(session, product["product_id"], size="M", color="floral")
            for product in results
        ]
    finally:
        session.close()

    assert "P001" not in product_ids
    assert len(results) == 3
    assert all(product["category"] == "dresses" for product in results)
    assert all(product["price"] <= 80 for product in results)
    assert all(stock["in_stock"] for stock in stock_results)


def test_similar_products_do_not_return_over_budget_products():
    session = SessionLocal()
    try:
        results = find_alternatives(
            session,
            "P001",
            size="M",
            color="floral",
            max_price=60,
            category="dresses",
            limit=3,
        )
    finally:
        session.close()

    assert [product["product_id"] for product in results] == ["P002"]
    assert all(product["price"] <= 60 for product in results)


def test_similar_products_return_no_results_for_unavailable_exact_variant():
    session = SessionLocal()
    try:
        results = find_alternatives(
            session,
            "P001",
            size="M",
            color="black",
            max_price=80,
            category="dresses",
            limit=3,
        )
    finally:
        session.close()

    assert results == []


def test_available_variant_returns_store_rows_with_variant_context():
    session = SessionLocal()
    try:
        result = check_store_stock(session, "P001", store_name="Maple Grove", size="S", color="black")
    finally:
        session.close()

    assert result["found"] is True
    assert result["variant_available"] is True
    assert result["stores"][0]["store_name"] == "Maple Grove"
    assert result["stores"][0]["size"] == "S"
    assert result["stores"][0]["color"] == "black"
    assert result["stores"][0]["in_stock"] is True


def test_black_medium_manual_cart_submit_is_rejected():
    result = add_to_cart(
        AddToCartRequest(product_id="P001", quantity=1, color="black", size="M")
    )

    assert result["success"] is False
    assert "Black Midi Dress in Black, size M is currently out of stock." == result["error"]


def test_switching_medium_to_large_changes_availability():
    medium = get_product_stock("P001", color="black", size="M")
    large = get_product_stock("P001", color="black", size="L")

    assert medium["in_stock"] is False
    assert medium["total_quantity"] == 0
    assert large["in_stock"] is True
    assert large["total_quantity"] == 3


def test_different_colors_can_have_different_inventory_for_same_size():
    black_medium = get_product_stock("P001", color="black", size="M")
    floral_medium = get_product_stock("P001", color="floral", size="M")

    assert black_medium["in_stock"] is False
    assert floral_medium["in_stock"] is True
    assert floral_medium["total_quantity"] > 0


def test_requested_quantity_over_available_inventory_is_rejected():
    result = add_to_cart(
        AddToCartRequest(product_id="P001", quantity=4, color="black", size="S")
    )

    assert result["success"] is False
    assert result["available_quantity"] == 3
    assert "Only 3 available" in result["error"]


def test_product_without_explicit_size_color_still_uses_aggregate_stock():
    result = add_to_cart(AddToCartRequest(product_id="P026", quantity=1))

    assert result["success"] is True
    assert result["product_id"] == "P026"
    assert result["size"] is None
    assert result["color"] is None


def test_invalid_size_color_combination_is_rejected():
    result = add_to_cart(
        AddToCartRequest(product_id="P001", quantity=1, color="navy", size="L")
    )

    assert result["success"] is False
    assert result["in_stock"] is False
    assert "Black Midi Dress in Navy, size L is currently out of stock." == result["error"]


def test_checkout_rejects_manually_submitted_unavailable_variant_before_payment():
    result = checkout(
        CheckoutRequest(
            items=[CheckoutItem(product_id="P001", quantity=1, color="black", size="M")]
        )
    )

    assert result["success"] is False
    assert result["error"] == "Black Midi Dress in Black, size M is currently out of stock."


def test_product_detail_exposes_variant_inventory():
    product = get_product_detail("P001")

    variants = {
        (variant["color"], variant["size"]): variant["quantity"]
        for variant in product["variants"]
    }
    assert variants[("black", "S")] == 3
    assert variants[("black", "M")] == 0
    assert variants[("black", "L")] == 3
