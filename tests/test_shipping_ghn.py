"""Giao Hàng Nhanh: báo phí theo địa chỉ mới, tạo vận đơn khi admin chuyển "Đang giao", webhook có xác minh lại."""
import importlib
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.core.config import settings
from app.core.security import create_access_token
from app.modules.shipping.ghn import GHNClient, GHNError, province_key, ward_key
from app.modules.shipping.provider import GHNShippingProvider, MockShippingProvider

pytestmark = pytest.mark.asyncio

SHIPPING_MODULES = ("app.modules.orders.router", "app.modules.shipping.router", "app.modules.shipping.tracking")

PROVINCES = [{"_id": 1000001, "name": "Hồ Chí Minh", "extension_names": ["TP. Hồ Chí Minh"], "status": 1},
             {"_id": 1000002, "name": "Đà Nẵng", "extension_names": [], "status": 1}]
WARDS = {1000001: [{"_id": 1, "name": "Phường Sài Gòn", "extension_names": [], "status": 1},
                   {"_id": 2, "name": "Phường Bến Thành", "extension_names": [], "status": 1}]}


class FakeGHN(GHNClient):
    def __init__(self):
        super().__init__("tok", "240288", "https://ghn.test")
        self.calls = []
        self.status = "ready_to_pick"
        self.fail_create = None

    async def _request(self, method, path, *, json=None, params=None):
        self.calls.append((path, json, params))
        if path.endswith("province/all"):
            return PROVINCES
        if path.endswith("all-by-province-id"):
            return WARDS.get(params["province_id"], []) if params["offset"] == 0 else []
        if path.endswith("/preview"):
            return {"total_fee": 31500}
        if path.endswith("/create"):
            if self.fail_create:
                raise GHNError(self.fail_create)
            return {"order_code": "GHN123ABC", "total_fee": 31500, "expected_delivery_time": "2026-11-25T00:00:00Z"}
        if path.endswith("/detail"):
            return {"order_code": params["order_code"], "status": self.status}
        raise AssertionError(path)


@pytest.fixture
def ghn(monkeypatch):
    fake = FakeGHN()
    provider = GHNShippingProvider(fake, MockShippingProvider())
    for module in (importlib.import_module(m) for m in SHIPPING_MODULES):
        monkeypatch.setattr(module, "shipping_service", provider)
    monkeypatch.setattr(settings, "GHN_SHOP_ID", "240288")
    return fake


def staff():
    return {"Authorization": f"Bearer {create_access_token(subject='admin-1', role='admin')}"}


@pytest_asyncio.fixture
async def processing_order(mock_mongo_db):
    doc = {
        "id": "ord-1", "order_code": "DT-20261122-AAAA", "customer_name": "Lan", "customer_phone": "0912345678",
        "shipping_address": {"recipient_name": "Lan", "phone": "0912345678", "street_address": "72 Lê Thánh Tôn",
                             "ward": "Sài Gòn", "city_province": "TP. Hồ Chí Minh"},
        "subtotal": 300000, "embroidery_total": 0, "shipping_fee": 31500, "total_amount": 331500,
        "payment_method": "COD", "payment_status": "unpaid", "status": "processing",
        "items": [{"product_id": "p1", "product_name": "Võ phục Vovinam", "sku": "VV-M", "size": "M", "price": 300000, "quantity": 2}],
        "status_history": [], "created_at": datetime.now(timezone.utc), "updated_at": datetime.now(timezone.utc),
    }
    await mock_mongo_db["orders"].insert_one(doc)
    return doc


@pytest.mark.asyncio
async def test_name_matching():
    assert province_key("TP. Hồ Chí Minh") == province_key("Hồ Chí Minh") == "ho chi minh"
    assert ward_key("Phường Sài Gòn") == ward_key("sài gòn") == "sai gon"
    assert ward_key("Xã Đông Hòa") == "dong hoa"


async def test_quote_uses_ghn_with_new_address_and_freeship(client, ghn):
    res = await client.get("/api/v1/orders/shipping-quote", params={"city_province": "TP. Hồ Chí Minh", "ward": "Sài Gòn", "subtotal": 300000, "quantity": 2})
    data = res.json()["data"]
    assert data["shipping_fee"] == 31500 and data["carrier"] == "GHN"
    body = next(c[1] for c in ghn.calls if c[0].endswith("/preview"))
    assert body["is_new_to_address"] is True
    assert body["to_province_name"] == "Hồ Chí Minh" and body["to_ward_name"] == "Phường Sài Gòn"
    assert body["weight"] == 1000

    # Đạt ngưỡng miễn phí thì không gọi GHN
    n = len(ghn.calls)
    res = await client.get("/api/v1/orders/shipping-quote", params={"city_province": "Đà Nẵng", "ward": "Hải Châu", "subtotal": 600000})
    assert res.json()["data"]["shipping_fee"] == 0 and len(ghn.calls) == n

    # Chưa có phường: phí tạm theo tỉnh (giả lập)
    res = await client.get("/api/v1/orders/shipping-quote", params={"city_province": "Đà Nẵng", "subtotal": 100000})
    assert res.json()["data"]["shipping_fee"] == 40000


async def test_wards_endpoint(client, ghn):
    res = await client.get("/api/v1/shipping/wards", params={"province": "TP. Hồ Chí Minh"})
    assert res.json()["data"] == ["Phường Bến Thành", "Phường Sài Gòn"]


async def test_shipping_creates_ghn_order_with_cod(client, mock_mongo_db, ghn, processing_order):
    res = await client.patch(f"/api/v1/orders/{processing_order['order_code']}/status", headers=staff(), json={"new_status": "shipping"})
    assert res.status_code == 200, res.text
    data = res.json()["data"]
    assert data["status"] == "shipping" and data["tracking_code"] == "GHN123ABC" and data["carrier"] == "GHN"
    assert data["shipment"]["status_label"] == "Chờ lấy hàng"

    body = next(c[1] for c in ghn.calls if c[0].endswith("/create"))
    assert body["client_order_code"] == processing_order["order_code"]
    assert body["cod_amount"] == 331500  # COD chưa thanh toán: GHN thu hộ toàn bộ
    assert body["is_new_to_address"] is True and body["to_ward_name"] == "Phường Sài Gòn"
    assert body["is_new_from_address"] is True


async def test_ghn_rejects_keeps_status(client, mock_mongo_db, ghn, processing_order):
    ghn.fail_create = "Phường không hợp lệ"
    res = await client.patch(f"/api/v1/orders/{processing_order['order_code']}/status", headers=staff(), json={"new_status": "shipping"})
    assert res.status_code == 502 and "Phường không hợp lệ" in res.text
    assert (await mock_mongo_db["orders"].find_one({"order_code": processing_order["order_code"]}))["status"] == "processing"


async def test_webhook_verifies_with_ghn_then_marks_delivered(client, mock_mongo_db, ghn, processing_order):
    await client.patch(f"/api/v1/orders/{processing_order['order_code']}/status", headers=staff(), json={"new_status": "shipping"})

    # Payload giả mạo "delivered" nhưng GHN thực tế vẫn đang giao -> không đổi sang Đã giao
    ghn.status = "delivering"
    res = await client.post("/api/v1/shipping/ghn/webhook", json={"ShopID": 240288, "OrderCode": "GHN123ABC", "Status": "delivered", "Type": "switch_status"})
    assert res.json()["status"] == "delivering"
    doc = await mock_mongo_db["orders"].find_one({"order_code": processing_order["order_code"]})
    assert doc["status"] == "shipping" and doc["shipment"]["status"] == "delivering"

    ghn.status = "delivered"
    await client.post("/api/v1/shipping/ghn/webhook", json={"ShopID": 240288, "OrderCode": "GHN123ABC", "Type": "switch_status"})
    doc = await mock_mongo_db["orders"].find_one({"order_code": processing_order["order_code"]})
    assert doc["status"] == "delivered" and doc["delivered_at"]
    assert [e["status"] for e in doc["shipment_events"]] == ["delivering", "delivered"]

    # Shop khác / mã lạ thì bỏ qua
    res = await client.post("/api/v1/shipping/ghn/webhook", json={"ShopID": 1, "OrderCode": "GHN123ABC"})
    assert res.json()["ok"] is False
    res = await client.post("/api/v1/shipping/ghn/webhook", json={"ShopID": 240288, "OrderCode": "NOPE"})
    assert res.json()["ok"] is False


async def test_order_detail_syncs_stale_shipment(client, mock_mongo_db, ghn, processing_order):
    await client.patch(f"/api/v1/orders/{processing_order['order_code']}/status", headers=staff(), json={"new_status": "shipping"})
    await mock_mongo_db["orders"].update_one(
        {"order_code": processing_order["order_code"]},
        {"$set": {"shipment.synced_at": datetime(2026, 1, 1, tzinfo=timezone.utc)}},
    )
    ghn.status = "delivered"
    res = await client.get(f"/api/v1/orders/{processing_order['order_code']}", params={"phone": "0912345678"})
    data = res.json()["data"]
    assert data["status"] == "delivered" and data["shipment"]["status_label"] == "Giao thành công"
