from scout.agents.verification import verify_price_grounding


def test_verify_price_grounding_accepts_tool_returned_price():
    products = [{"product_id": "P001", "price": 79.99}]

    passed, reason = verify_price_grounding("This one is $79.99.", products)

    assert passed, reason


def test_verify_price_grounding_accepts_customer_stated_budget():
    products = [{"product_id": "P001", "price": 79.99}]

    passed, reason = verify_price_grounding(
        "This fits your $100 budget.", products, customer_message="Find a dress under $100"
    )

    assert passed, reason


def test_verify_price_grounding_accepts_known_policy_prices():
    products = [{"product_id": "P001", "price": 79.99}]

    passed, reason = verify_price_grounding(
        "Standard shipping is free over $75.00, or express is $14.99.", products
    )

    assert passed, reason


def test_verify_price_grounding_rejects_unsupported_invented_price():
    products = [{"product_id": "P001", "price": 79.99}]

    passed, reason = verify_price_grounding("This one is $49.99.", products)

    assert not passed
    assert "49.99" in reason
