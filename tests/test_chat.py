"""Chat trực tuyến khách hàng <-> cửa hàng."""
import pytest
import pytest_asyncio

from app.core.security import create_access_token

pytestmark = pytest.mark.asyncio


def bearer(sub, role):
    return {"Authorization": f"Bearer {create_access_token(subject=sub, role=role)}"}


@pytest_asyncio.fixture
async def users(mock_mongo_db):
    await mock_mongo_db["users"].insert_many([
        {"id": "cus-1", "full_name": "Chị Lan", "phone": "0912345678", "email": "lan@x.vn", "role": "customer", "is_active": True},
        {"id": "staff-1", "full_name": "NV Hùng", "phone": "0900000001", "role": "staff", "is_active": True},
    ])


async def test_guest_flow_and_staff_reply(client, mock_mongo_db, users):
    # Khách vãng lai chưa có hội thoại
    res = await client.get("/api/v1/chat/me")
    assert res.json()["data"]["conversation"] is None

    # Thiếu tên -> từ chối
    res = await client.post("/api/v1/chat/me/messages", json={"body": "Còn size 150 không?"})
    assert res.status_code == 400

    res = await client.post("/api/v1/chat/me/messages", json={"body": "Còn size 150 không?", "name": "Anh Tú", "phone": "0987 654 321"})
    data = res.json()["data"]
    guest_token = data["guest_token"]
    assert guest_token and data["messages"][0]["seq"] == 1
    conv_id = data["conversation"]["id"]
    assert data["conversation"]["is_guest"] and data["conversation"]["customer_phone"] == "0987654321"

    stored = await mock_mongo_db["conversations"].find_one({"id": conv_id})
    assert "user_id" not in stored and stored["guest_token_hash"] != guest_token

    # Token khác không đọc được hội thoại
    res = await client.get("/api/v1/chat/me", headers={"X-Chat-Token": "wrong"})
    assert res.json()["data"]["conversation"] is None

    # Nhân viên thấy hội thoại + badge chưa đọc
    staff = bearer("staff-1", "staff")
    unread = (await client.get("/api/v1/admin/chat/unread", headers=staff)).json()["data"]
    assert unread == {"conversations": 1, "messages": 1}
    convs = (await client.get("/api/v1/admin/chat/conversations", headers=staff)).json()["data"]
    assert [c["id"] for c in convs] == [conv_id]

    res = await client.post(f"/api/v1/admin/chat/conversations/{conv_id}/messages", json={"body": "Dạ còn ạ"}, headers=staff)
    assert res.json()["data"]["sender_name"] == "NV Hùng" and res.json()["data"]["seq"] == 2

    # Khách polling nhận tin mới sau seq 1, có 1 tin chưa đọc
    mine = (await client.get("/api/v1/chat/me", params={"after_seq": 1}, headers={"X-Chat-Token": guest_token})).json()["data"]
    assert [m["body"] for m in mine["messages"]] == ["Dạ còn ạ"]
    assert mine["conversation"]["unread_for_customer"] == 1 and mine["conversation"]["unread_for_staff"] == 0

    await client.post("/api/v1/chat/me/read", headers={"X-Chat-Token": guest_token})
    mine = (await client.get("/api/v1/chat/me", params={"limit": 0}, headers={"X-Chat-Token": guest_token})).json()["data"]
    assert mine["conversation"]["unread_for_customer"] == 0 and mine["messages"] == []


async def test_logged_in_customer_and_guest_claim(client, mock_mongo_db, users):
    res = await client.post("/api/v1/chat/me/messages", json={"body": "Hi", "name": "Lan khách"})
    guest_token = res.json()["data"]["guest_token"]

    cus = {**bearer("cus-1", "customer"), "X-Chat-Token": guest_token}
    res = await client.post("/api/v1/chat/me/messages", json={"body": "Mình vừa đăng nhập"}, headers=cus)
    data = res.json()["data"]
    assert data["guest_token"] is None
    assert data["conversation"]["is_guest"] is False and data["conversation"]["customer_name"] == "Chị Lan"
    assert data["conversation"]["message_count"] == 2
    assert await mock_mongo_db["conversations"].count_documents({}) == 1

    # Khách đăng nhập không cần guest token nữa
    mine = (await client.get("/api/v1/chat/me", headers=bearer("cus-1", "customer"))).json()["data"]
    assert [m["body"] for m in mine["messages"]] == ["Hi", "Mình vừa đăng nhập"]


async def test_two_guests_and_customer_cannot_use_admin(client, mock_mongo_db, users):
    await client.post("/api/v1/chat/me/messages", json={"body": "a", "name": "Khách A"})
    await client.post("/api/v1/chat/me/messages", json={"body": "b", "name": "Khách B"})
    assert await mock_mongo_db["conversations"].count_documents({}) == 2

    res = await client.get("/api/v1/admin/chat/conversations", headers=bearer("cus-1", "customer"))
    assert res.status_code == 403
    res = await client.get("/api/v1/admin/chat/conversations")
    assert res.status_code == 401


async def test_close_and_reopen_on_customer_message(client, users):
    res = await client.post("/api/v1/chat/me/messages", json={"body": "x"}, headers=bearer("cus-1", "customer"))
    conv_id = res.json()["data"]["conversation"]["id"]
    staff = bearer("staff-1", "staff")

    res = await client.patch(f"/api/v1/admin/chat/conversations/{conv_id}", json={"status": "closed"}, headers=staff)
    assert res.json()["data"]["status"] == "closed"
    closed = (await client.get("/api/v1/admin/chat/conversations", params={"status": "closed"}, headers=staff)).json()["data"]
    assert len(closed) == 1

    await client.post("/api/v1/chat/me/messages", json={"body": "còn đó không"}, headers=bearer("cus-1", "customer"))
    convs = (await client.get("/api/v1/admin/chat/conversations", params={"status": "open"}, headers=staff)).json()["data"]
    assert convs[0]["status"] == "open" and convs[0]["unread_for_staff"] == 2


async def test_message_validation(client, users):
    cus = bearer("cus-1", "customer")
    assert (await client.post("/api/v1/chat/me/messages", json={"body": "   \n  "}, headers=cus)).status_code == 400
    assert (await client.post("/api/v1/chat/me/messages", json={"body": "a" * 2001}, headers=cus)).status_code == 422
    res = await client.post("/api/v1/admin/chat/conversations/nope/messages", json={"body": "x"}, headers=bearer("staff-1", "staff"))
    assert res.status_code == 404
