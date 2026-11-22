import pytest
from httpx import AsyncClient

from app.core.security import create_access_token


def _h(role: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token(subject=f'usr-{role}', role=role)}"}


@pytest.mark.asyncio
async def test_store_settings_defaults_and_update(client: AsyncClient, mock_mongo_db):
    res = await client.get("/api/v1/settings/store")
    assert res.status_code == 200
    assert res.json()["data"]["hotline"] == "1900 6868"

    body = {**res.json()["data"], "hotline": "0909 123 456", "address": "12 Lê Lợi, Quận 1, TP. Hồ Chí Minh", "zalo": ""}
    assert (await client.put("/api/v1/settings/store", json=body)).status_code == 401
    assert (await client.put("/api/v1/settings/store", json=body, headers=_h("staff"))).status_code == 403

    ok = await client.put("/api/v1/settings/store", json=body, headers=_h("admin"))
    assert ok.status_code == 200, ok.text
    assert ok.json()["data"]["zalo"] is None

    again = (await client.get("/api/v1/settings/store")).json()["data"]
    assert again["hotline"] == "0909 123 456" and again["address"].startswith("12 Lê Lợi")
    log = await mock_mongo_db["activity_logs"].find_one({"action": "UPDATE_STORE_SETTINGS"})
    assert log and "hotline" in log["details"]


@pytest.mark.asyncio
async def test_store_settings_validation(client: AsyncClient):
    base = (await client.get("/api/v1/settings/store")).json()["data"]
    for bad in ({"hotline": "abc"}, {"hotline": ""}, {"email": "khong-phai-email"}, {"facebook_url": "facebook.com/x"}):
        res = await client.put("/api/v1/settings/store", json={**base, **bad}, headers=_h("admin"))
        assert res.status_code == 422, bad


@pytest.mark.asyncio
async def test_variant_price_change_is_logged(client: AsyncClient, mock_mongo_db):
    await mock_mongo_db["products"].insert_one({
        "id": "p1", "slug": "p1", "name": "Võ phục", "price": 170000,
        "variants": [{"sku": "S1", "size": "Size 1", "color": "Trắng", "price": 170000, "stock": 5},
                     {"sku": "S2", "size": "Size 2", "color": "Trắng", "price": 175000, "stock": 5}],
    })
    variants = [{"sku": "S1", "size": "Size 1", "color": "Trắng", "price": 160000, "stock": 5},
                {"sku": "S2", "size": "Size 2", "color": "Trắng", "price": 175000, "stock": 5}]
    res = await client.put("/api/v1/products/p1", json={"price": 160000, "variants": variants}, headers=_h("admin"))
    assert res.status_code == 200, res.text
    assert res.json()["data"]["variants"][0]["price"] == 160000
    log = await mock_mongo_db["activity_logs"].find_one({"action": "UPDATE_VARIANT_PRICE"})
    assert log["details"]["changes"] == [{"sku": "S1", "size": "Size 1", "old_price": 170000, "new_price": 160000}]


@pytest.mark.asyncio
async def test_patch_prices_keeps_stock(client: AsyncClient, mock_mongo_db):
    await mock_mongo_db["products"].insert_one({
        "id": "p2", "slug": "p2", "name": "Đai", "price": 35000, "original_price": None,
        "variants": [{"sku": "A", "size": "Size 1", "color": "Trắng", "price": 35000, "stock": 7},
                     {"sku": "B", "size": "Size 2", "color": "Trắng", "price": 40000, "stock": 3}],
    })
    url = "/api/v1/products/p2/prices"
    assert (await client.patch(url, json={"prices": {"A": 30000}}, headers=_h("customer"))).status_code == 403
    assert (await client.patch(url, json={"prices": {"X": 30000}}, headers=_h("admin"))).status_code == 400
    assert (await client.patch(url, json={"prices": {"A": 30000}, "original_price": 20000}, headers=_h("admin"))).status_code == 400

    res = await client.patch(url, json={"prices": {"A": 30000, "B": 45000}, "original_price": 40000}, headers=_h("staff"))
    assert res.status_code == 200, res.text
    d = res.json()["data"]
    assert d["price"] == 30000 and d["original_price"] == 40000 and d["discount_percent"] == 25
    assert [(v["price"], v["stock"]) for v in d["variants"]] == [(30000, 7), (45000, 3)]

    cleared = (await client.patch(url, json={"prices": {"A": 30000}, "original_price": 0}, headers=_h("admin"))).json()["data"]
    assert cleared["original_price"] is None and cleared["discount_percent"] == 0
