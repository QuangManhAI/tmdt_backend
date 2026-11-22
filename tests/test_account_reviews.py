"""Tài khoản khách (thông tin, gắn đơn khi chưa đăng nhập) và đánh giá chỉ dành cho người đã mua."""
from datetime import datetime, timezone

import pytest
import pytest_asyncio

from app.core.security import create_access_token

pytestmark = pytest.mark.asyncio


def bearer(sub, role="customer"):
    return {"Authorization": f"Bearer {create_access_token(subject=sub, role=role)}"}


def order(code, phone, status, user_id=None, product_id="prod-1", size="M"):
    doc = {
        "id": f"id-{code}",
        "order_code": code,
        "customer_name": "Khách",
        "customer_phone": phone,
        "status": status,
        "total_amount": 100000,
        "items": [{"product_id": product_id, "product_name": "Võ phục", "sku": "SKU", "size": size, "price": 100000, "quantity": 1}],
        "created_at": datetime.now(timezone.utc),
        "updated_at": datetime.now(timezone.utc),
    }
    if user_id:
        doc["user_id"] = user_id
    return doc


@pytest_asyncio.fixture
async def seed(mock_mongo_db):
    await mock_mongo_db["users"].insert_many([
        {"id": "u-1", "full_name": "Chị Lan", "phone": "0912345678", "role": "customer", "is_active": True},
        {"id": "u-g", "full_name": "Google User", "google_id": "g-1", "email": "g@gmail.com", "role": "customer", "is_active": True},
        {"id": "u-2", "full_name": "Anh Nam", "phone": "0987654321", "role": "customer", "is_active": True},
    ])
    await mock_mongo_db["products"].insert_one({"id": "prod-1", "slug": "vo-phuc", "name": "Võ phục", "images": ["/a.jpg"]})


# --------------------------------------------------------------------------- tài khoản

async def test_profile_add_phone_and_default_address(client, mock_mongo_db, seed):
    res = await client.get("/api/v1/account/profile", headers=bearer("u-g"))
    assert res.json()["data"]["phone"] is None and res.json()["data"]["linked_google"] is True

    res = await client.patch("/api/v1/account/profile", headers=bearer("u-g"), json={
        "phone": "0901 234 567",
        "default_address": {"recipient_name": "G", "phone": "0901234567", "street_address": "1 Lê Lợi", "city_province": "Huế"},
    })
    data = res.json()["data"]
    assert data["phone"] == "0901234567" and data["default_address"]["city_province"] == "Huế"

    # SĐT đã thuộc tài khoản khác
    res = await client.patch("/api/v1/account/profile", headers=bearer("u-g"), json={"phone": "0912345678"})
    assert res.status_code == 400
    # SĐT sai định dạng
    res = await client.patch("/api/v1/account/profile", headers=bearer("u-g"), json={"phone": "12345"})
    assert res.status_code == 400
    # Tài khoản đăng ký bằng SĐT không được xóa SĐT
    res = await client.patch("/api/v1/account/profile", headers=bearer("u-1"), json={"phone": ""})
    assert res.status_code == 400


async def test_claim_orders_requires_matching_phone(client, mock_mongo_db, seed):
    await mock_mongo_db["orders"].insert_many([
        order("DT-A", "0911111111", "confirmed"),
        order("DT-B", "0922222222", "confirmed"),
        order("DT-C", "0933333333", "confirmed", user_id="u-2"),
    ])
    res = await client.post("/api/v1/account/claim-orders", headers=bearer("u-1"), json={"orders": [
        {"code": "dt-a", "phone": "0911 111 111"},   # đúng -> gắn
        {"code": "DT-B", "phone": "0900000000"},     # sai SĐT -> bỏ qua
        {"code": "DT-C", "phone": "0933333333"},     # đã thuộc tài khoản khác -> bỏ qua
        {"code": "DT-X", "phone": "0900000000"},     # không tồn tại
    ]})
    assert res.json()["data"]["claimed"] == ["DT-A"]
    assert (await mock_mongo_db["orders"].find_one({"order_code": "DT-A"}))["user_id"] == "u-1"
    assert "user_id" not in await mock_mongo_db["orders"].find_one({"order_code": "DT-B"})
    assert (await mock_mongo_db["orders"].find_one({"order_code": "DT-C"}))["user_id"] == "u-2"

    # Đơn đã gắn hiện trong danh sách đơn của tài khoản
    mine = (await client.get("/api/v1/orders", headers=bearer("u-1"))).json()["data"]
    assert [o["order_code"] for o in mine] == ["DT-A"]

    assert (await client.post("/api/v1/account/claim-orders", json={"orders": []})).status_code == 401


# --------------------------------------------------------------------------- đánh giá

async def test_only_buyers_who_received_can_review(client, mock_mongo_db, seed):
    payload = {"product_id": "prod-1", "rating": 4, "comment": "Vải đẹp", "size_fit": "fit"}

    # Chưa mua
    assert (await client.post("/api/v1/reviews", headers=bearer("u-1"), json=payload)).status_code == 403
    # Đã mua nhưng chưa giao
    await mock_mongo_db["orders"].insert_one(order("DT-1", "0912345678", "shipping", user_id="u-1"))
    assert (await client.post("/api/v1/reviews", headers=bearer("u-1"), json=payload)).status_code == 403
    # Chưa đăng nhập
    assert (await client.post("/api/v1/reviews", json=payload)).status_code == 401

    await mock_mongo_db["orders"].update_one({"order_code": "DT-1"}, {"$set": {"status": "delivered"}})
    res = await client.post("/api/v1/reviews", headers=bearer("u-1"), json=payload)
    assert res.status_code == 200
    rev = res.json()["data"]
    assert rev["verified_purchase"] is True and rev["bought_size"] == "M" and rev["user_name"] == "Chị Lan"

    # Gửi lại = sửa, không tạo đánh giá thứ hai
    res = await client.post("/api/v1/reviews", headers=bearer("u-1"), json={**payload, "rating": 5, "comment": "Rất ưng"})
    assert res.json()["data"]["id"] == rev["id"] and res.json()["data"]["rating"] == 5
    assert await mock_mongo_db["reviews"].count_documents({}) == 1

    mine = (await client.get("/api/v1/reviews/me", headers=bearer("u-1"))).json()["data"]
    assert mine[0]["product_slug"] == "vo-phuc" and mine[0]["review"]["comment"] == "Rất ưng"


async def test_admin_hide_review(client, mock_mongo_db, seed):
    await mock_mongo_db["orders"].insert_one(order("DT-1", "0912345678", "completed", user_id="u-1"))
    rev = (await client.post("/api/v1/reviews", headers=bearer("u-1"), json={"product_id": "prod-1", "rating": 1, "comment": "spam"})).json()["data"]

    assert (await client.patch(f"/api/v1/reviews/{rev['id']}/visibility", headers=bearer("u-1"), json={"hidden": True})).status_code == 403

    staff = bearer("staff-1", "staff")
    res = await client.patch(f"/api/v1/reviews/{rev['id']}/visibility", headers=staff, json={"hidden": True})
    assert res.status_code == 200
    public = (await client.get("/api/v1/reviews", params={"product_id": "prod-1"})).json()["data"]
    assert public == []

    hidden = (await client.get("/api/v1/reviews/admin/list", params={"visibility": "hidden"}, headers=staff)).json()["data"]
    assert hidden[0]["id"] == rev["id"] and hidden[0]["product_name"] == "Võ phục"

    await client.patch(f"/api/v1/reviews/{rev['id']}/visibility", headers=staff, json={"hidden": False})
    assert len((await client.get("/api/v1/reviews", params={"product_id": "prod-1"})).json()["data"]) == 1
