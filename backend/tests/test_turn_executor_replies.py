from scout.agents.intent_splitter import StructuredIntent
from scout.agents.orchestration.turn_executor import _similar_products_no_match_reply


def test_cheaper_options_no_match_compares_current_recommendations_first():
    intent = StructuredIntent(
        text="Find similar Scout products for product_id P004 (Slip Dress) in dresses under $80 with cheaper options.",
        request_type="similar_products",
        confidence=0.96,
        product_type="dresses",
        budget_max=80,
        product_id="P004",
    )
    context = {
        "active_selected_products": [
            {
                "product_id": "P001",
                "name": "Black Midi Dress",
                "price": 79.99,
                "promotion": {"discounted_price": 67.99},
            },
            {
                "product_id": "P004",
                "name": "Slip Dress",
                "price": 62.50,
                "promotion": {"discounted_price": 53.12},
            },
        ]
    }

    reply = _similar_products_no_match_reply(intent, context)

    assert reply == (
        "Slip Dress is already the cheapest of these at $62.50, with a sale price of $53.12. "
        "I don’t see another similar option under $80 right now, but I can keep looking with a lower budget."
    )


def test_regular_similar_no_match_keeps_product_specific_reply():
    intent = StructuredIntent(
        text="Find similar Scout products for product_id P004 (Slip Dress) in dresses color black size M under $80.",
        request_type="similar_products",
        confidence=0.96,
        product_type="dresses",
        budget_max=80,
        color="black",
        size="M",
        product_id="P004",
    )

    assert _similar_products_no_match_reply(intent, {}) == (
        "I don’t see another option like Slip Dress that matches black, medium, under $80 right now."
    )
