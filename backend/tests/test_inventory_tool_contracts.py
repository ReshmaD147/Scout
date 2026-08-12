from types import SimpleNamespace

from scout.services import product_service, store_service


class FakeProductRepository:
    def __init__(self, _session):
        pass

    def get_by_id(self, product_id):
        if product_id == "missing":
            return None
        return SimpleNamespace(product_id=product_id)


class FakeStockRepository:
    def __init__(self, _session):
        pass

    def get_variants(self, product_id, size=None, color=None):
        if product_id == "missing":
            return []
        variants = [
            SimpleNamespace(size="M", color="black", quantity=0),
            SimpleNamespace(size="L", color="black", quantity=3),
        ]
        if size:
            variants = [variant for variant in variants if variant.size == size]
        if color:
            variants = [variant for variant in variants if variant.color == color]
        return variants


class FakeStoreRepository:
    def __init__(self, _session):
        pass

    def get_stock_for_product(self, product_id, store_name=None):
        rows = [
            (
                SimpleNamespace(product_id=product_id, store_id="S001", quantity=2),
                SimpleNamespace(store_id="S001", name="Maple Grove", address="1 Main"),
            ),
            (
                SimpleNamespace(product_id=product_id, store_id="S002", quantity=0),
                SimpleNamespace(store_id="S002", name="Downtown", address="2 Main"),
            ),
        ]
        if store_name:
            rows = [
                row for row in rows if row[1].name.casefold() == store_name.casefold()
            ]
        return rows


def test_stock_matching_size_returns_structured_zero_quantity_variant(monkeypatch):
    monkeypatch.setattr(product_service, "ProductRepository", FakeProductRepository)
    monkeypatch.setattr(product_service, "StockRepository", FakeStockRepository)

    result = product_service.check_stock(object(), "P001", size="medium", color="black")

    assert result["product_id"] == "P001"
    assert result["found"] is True
    assert result["requested_size"] == "M"
    assert result["requested_color"] == "black"
    assert result["in_stock"] is False
    assert result["total_quantity"] == 0
    assert result["variants"] == [
        {"size": "M", "color": "black", "quantity": 0, "in_stock": False}
    ]


def test_stock_positive_quantity_returns_in_stock_true(monkeypatch):
    monkeypatch.setattr(product_service, "ProductRepository", FakeProductRepository)
    monkeypatch.setattr(product_service, "StockRepository", FakeStockRepository)

    result = product_service.check_stock(object(), "P001", size="L", color="black")

    assert result["in_stock"] is True
    assert result["total_quantity"] == 3
    assert result["variants"][0]["in_stock"] is True


def test_stock_wrong_size_returns_structured_no_match(monkeypatch):
    monkeypatch.setattr(product_service, "ProductRepository", FakeProductRepository)
    monkeypatch.setattr(product_service, "StockRepository", FakeStockRepository)

    result = product_service.check_stock(object(), "P001", size="XS")

    assert result == {
        "product_id": "P001",
        "found": True,
        "requested_size": "XS",
        "requested_color": None,
        "in_stock": False,
        "total_quantity": 0,
        "variants": [],
    }


def test_stock_unknown_product_returns_structured_not_found(monkeypatch):
    monkeypatch.setattr(product_service, "ProductRepository", FakeProductRepository)
    monkeypatch.setattr(product_service, "StockRepository", FakeStockRepository)

    result = product_service.check_stock(object(), "missing", size="M")

    assert result == {
        "product_id": "missing",
        "found": False,
        "requested_size": "M",
        "requested_color": None,
        "in_stock": False,
        "total_quantity": 0,
        "variants": [],
    }


def test_store_exact_requested_store_returns_structured_row(monkeypatch):
    monkeypatch.setattr(store_service, "ProductRepository", FakeProductRepository)
    monkeypatch.setattr(store_service, "StoreRepository", FakeStoreRepository)

    result = store_service.check_store_stock(object(), "P001", store_name="Maple Grove")

    assert result["product_id"] == "P001"
    assert result["found"] is True
    assert result["requested_store_name"] == "Maple Grove"
    assert result["requested_store_had_no_stock"] is False
    assert result["stores"] == [
        {
            "store_id": "S001",
            "store_name": "Maple Grove",
            "address": "1 Main",
            "quantity": 2,
            "in_stock": True,
        }
    ]
    assert "pickup_available" not in result["stores"][0]


def test_store_wrong_store_returns_structured_empty_result(monkeypatch):
    monkeypatch.setattr(store_service, "ProductRepository", FakeProductRepository)
    monkeypatch.setattr(store_service, "StoreRepository", FakeStoreRepository)

    result = store_service.check_store_stock(object(), "P001", store_name="Unknown")

    assert result["product_id"] == "P001"
    assert result["found"] is True
    assert result["requested_store_name"] == "Unknown"
    assert result["stores"] == []
    assert result["requested_store_had_no_stock"] is False


def test_store_unknown_product_returns_structured_not_found(monkeypatch):
    monkeypatch.setattr(store_service, "ProductRepository", FakeProductRepository)
    monkeypatch.setattr(store_service, "StoreRepository", FakeStoreRepository)

    result = store_service.check_store_stock(object(), "missing", store_name="Maple Grove")

    assert result == {
        "product_id": "missing",
        "found": False,
        "requested_store_name": "Maple Grove",
        "stores": [],
    }
