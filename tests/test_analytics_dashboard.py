from datetime import datetime, timedelta, timezone

import pytest
from httpx import AsyncClient

from app.core.security import create_access_token
from app.modules.analytics.service import VN_TZ

URL = "/api/v1/analytics/dashboard"


def _headers(role: str) -> dict:
    return {"Authorization": f"Bearer {create_access_token(subject=f'usr-{role}', role=role)}"}


def _utc(local: datetime) -> datetime:
    """Giờ VN -> UTC không kèm tz, đúng như pymongo lưu."""
    return local.replace(tzinfo=VN_TZ).astimezone(timezone.utc).replace(tzinfo=None)


async def _seed(db):
    await db["products"].insert_many([
        {"id": "p-vp", "slug": "p-vp", "name": "Võ phục Vovinam", "category_id": "vo_phuc", "martial_art_id": "vovinam", "price": 170000,
         "variants": [{"sku": "VP-S1", "size": "Size 1", "price": 170000, "stock": 10}, {"sku": "VP-S2", "size": "Size 2", "price": 175000, "stock": 5}]},
        {"id": "p-dai", "slug": "p-dai", "name": "Đai Karate", "category_id": "dai", "martial_art_id": "karate", "price": 35000,
         "variants": [{"sku": "DAI", "size": "Freesize", "price": 35000, "stock": 20}]},
        {"id": "p-tui", "slug": "p-tui", "name": "Balo", "category_id": "tui_dung", "martial_art_id": "chung", "price": 180000,
         "variants": [{"sku": "TUI", "size": "Freesize", "price": 180000, "stock": 0}]},
    ])
    base = datetime(2026, 11, 10, 9, 30)
    orders = [
        # Kỳ hiện tại (01/11 - 10/11)
        {"status": "completed", "created_at": _utc(base), "payment_method": "COD", "customer_phone": "0901",
         "shipping_address": {"city_province": "Hà Nội"},
         "items": [{"product_id": "p-vp", "size": "Size 1", "price": 170000, "quantity": 2},
                   {"product_id": "p-dai", "size": "Freesize", "price": 35000, "quantity": 1}]},
        {"status": "paid", "created_at": _utc(base - timedelta(days=3)), "payment_method": "MOMO", "customer_phone": "0902",
         "shipping_address": {"city_province": "TP. Hồ Chí Minh"},
         "items": [{"product_id": "p-vp", "size": "Size 2", "price": 175000, "quantity": 1, "embroidery_fee": 30000}]},
        {"status": "confirmed", "created_at": _utc(base - timedelta(days=1)), "payment_method": "COD", "customer_phone": "0903",
         "items": [{"product_id": "p-tui", "size": "Freesize", "price": 180000, "quantity": 1}]},
        {"status": "cancelled", "created_at": _utc(base - timedelta(days=2)), "payment_method": "COD", "customer_phone": "0904",
         "items": [{"product_id": "p-dai", "size": "Freesize", "price": 35000, "quantity": 3}]},
        # Kỳ trước (22/10 - 31/10)
        {"status": "delivered", "created_at": _utc(datetime(2026, 10, 31, 20, 0)), "payment_method": "COD", "customer_phone": "0905",
         "items": [{"product_id": "p-vp", "size": "Size 1", "price": 170000, "quantity": 1}]},
        # Ngoài cả hai kỳ
        {"status": "completed", "created_at": _utc(datetime(2026, 9, 1, 8, 0)), "payment_method": "COD",
         "items": [{"product_id": "p-vp", "size": "Size 1", "price": 170000, "quantity": 9}]},
    ]
    for i, o in enumerate(orders):
        o.update({"id": f"o{i}", "order_code": f"DT-{i}", "channel": "retail"})
    await db["orders"].insert_many(orders)


@pytest.mark.asyncio
async def test_analytics_requires_admin(client: AsyncClient):
    assert (await client.get(URL)).status_code == 401
    assert (await client.get(URL, headers=_headers("staff"))).status_code == 403


@pytest.mark.asyncio
async def test_analytics_dashboard_aggregates(client: AsyncClient, mock_mongo_db):
    await _seed(mock_mongo_db)
    res = await client.get(URL, params={"start": "2026-11-01", "end": "2026-11-10"}, headers=_headers("admin"))
    assert res.status_code == 200, res.text
    d = res.json()["data"]

    assert d["filters"]["granularity"] == "day"
    s = d["summary"]
    # Đơn completed + paid: 2*170k + 35k + (175k + 30k thêu)
    assert s["revenue"]["value"] == 580000
    assert s["revenue"]["previous"] == 170000
    assert s["orders"]["value"] == 2
    assert s["units"]["value"] == 4
    assert s["customers"]["value"] == 2
    assert s["cancel_rate"]["value"] == 25.0  # 1 hủy / 4 đơn

    ts = {p["period"]: p for p in d["timeseries"]}
    assert len(ts) == 10
    assert ts["2026-11-10"]["revenue"] == 375000
    assert ts["2026-11-10"]["units_by_category"] == {"vo_phuc": 2, "dai": 1}
    # 31/10 dịch 10 ngày -> 10/11
    assert ts["2026-11-10"]["previous_revenue"] == 170000

    cats = {c["id"]: c for c in d["by_category"]}
    assert cats["vo_phuc"]["revenue"] == 545000 and cats["dai"]["units"] == 1
    assert cats["tui_dung"]["revenue"] == 0

    prods = {p["product_id"]: p for p in d["products"]}
    assert prods["p-vp"]["units"] == 3 and prods["p-vp"]["stock"] == 15
    assert prods["p-vp"]["sell_through"] == 16.7
    assert prods["p-tui"]["units"] == 0

    assert [r["id"] for r in d["sizes"]] == ["Size 1", "Size 2"]
    statuses = {r["id"]: r["count"] for r in d["statuses"]}
    assert statuses == {"paid": 1, "confirmed": 1, "completed": 1, "cancelled": 1}
    assert {r["id"] for r in d["payment_methods"]} == {"COD", "MOMO"}
    assert d["provinces"][0]["name"] == "Hà Nội"
    assert {"weekday": 1, "hour": 9, "orders": 1} in d["heatmap"]  # 10/11/2026 là thứ Ba


@pytest.mark.asyncio
async def test_analytics_filters_and_scope(client: AsyncClient, mock_mongo_db):
    await _seed(mock_mongo_db)
    h = _headers("admin")
    params = {"start": "2026-11-01", "end": "2026-11-10"}

    only_dai = (await client.get(URL, params={**params, "category": "dai"}, headers=h)).json()["data"]
    assert only_dai["summary"]["revenue"]["value"] == 35000
    assert [p["product_id"] for p in only_dai["products"]] == ["p-dai"]

    all_scope = (await client.get(URL, params={**params, "scope": "all"}, headers=h)).json()["data"]
    assert all_scope["summary"]["orders"]["value"] == 3  # thêm đơn confirmed

    momo = (await client.get(URL, params={**params, "payment_method": "MOMO"}, headers=h)).json()["data"]
    assert momo["summary"]["orders"]["value"] == 1

    monthly = (await client.get(URL, params={"start": "2026-01-01", "end": "2026-11-30"}, headers=h)).json()["data"]
    assert monthly["filters"]["granularity"] == "month"
    assert len(monthly["timeseries"]) == 11
