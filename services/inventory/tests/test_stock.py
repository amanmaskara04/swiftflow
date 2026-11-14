from app.stock import StockChange, cache_key, pick_order


def test_pick_order_prefers_most_available_then_code():
    c = [{"code": "B", "available": 5}, {"code": "A", "available": 5}, {"code": "C", "available": 9}]
    assert [x["code"] for x in pick_order(c)] == ["C", "A", "B"]


def test_stock_change_payload_matches_contract():
    from swiftflow_common.events import build_envelope

    payload = StockChange("TEE", "BLR-1", 10, 4, "reservation").payload()
    assert payload["available"] == 6
    build_envelope("StockLevelChanged", tenant_id="6f1d2c3b-4a59-4e6f-8a7b-9c0d1e2f3a4b",
                   aggregate_id="0b6e5f4a-3c2d-4b1a-9e8f-7d6c5b4a3f2e", producer="inventory", payload=payload)


def test_cache_key_is_per_tenant():
    assert cache_key("a", "SKU") != cache_key("b", "SKU")
