"""Đăng nhập Google / Facebook (OAuth). Gọi ra provider được thay bằng hàm giả."""
from urllib.parse import parse_qs, urlparse

import pytest

from app.core.config import settings
from app.core.security import decode_token
from app.modules.auth import oauth
from app.modules.auth.oauth import OAuthProfile

pytestmark = pytest.mark.asyncio

BASE = "/api/v1/auth/oauth"


@pytest.fixture(autouse=True)
def configure_providers(monkeypatch):
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_ID", "gid")
    monkeypatch.setattr(settings, "GOOGLE_CLIENT_SECRET", "gsecret")
    monkeypatch.setattr(settings, "FACEBOOK_APP_ID", "fid")
    monkeypatch.setattr(settings, "FACEBOOK_APP_SECRET", "fsecret")
    monkeypatch.setattr(settings, "FRONTEND_URL", "http://front.test")
    monkeypatch.setattr(settings, "BACKEND_URL", "http://testserver")


def fake_profile(monkeypatch, provider, subject, email, name="Nguyễn Văn A"):
    async def _fake(code):
        assert code == "the-code"
        return OAuthProfile(provider=provider, subject=subject, email=email, full_name=name)

    monkeypatch.setattr(oauth, f"_{provider}_profile", _fake)


async def run_flow(client, provider, next_path="/orders"):
    start = await client.get(f"{BASE}/{provider}/start", params={"next": next_path})
    assert start.status_code == 302
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    cb = await client.get(f"{BASE}/{provider}/callback", params={"code": "the-code", "state": state})
    assert cb.status_code == 302
    return cb.headers["location"]


def token_from(location):
    assert location.startswith("http://front.test/auth/callback#")
    frag = parse_qs(urlparse(location).fragment)
    return frag["token"][0], frag.get("next", [""])[0]


async def test_providers_endpoint(client):
    res = await client.get(f"{BASE}/providers")
    assert res.json()["data"] == {"google": True, "facebook": True}


async def test_start_redirects_to_google_with_state_cookie(client):
    res = await client.get(f"{BASE}/google/start")
    assert res.status_code == 302
    loc = urlparse(res.headers["location"])
    assert loc.netloc == "accounts.google.com"
    q = parse_qs(loc.query)
    assert q["client_id"] == ["gid"]
    assert q["redirect_uri"][0].endswith("/api/v1/auth/oauth/google/callback")
    assert "oauth_nonce" in res.headers.get("set-cookie", "")


async def test_google_creates_customer_without_phone(client, mock_mongo_db, monkeypatch):
    fake_profile(monkeypatch, "google", "g-1", "a@gmail.com")
    location = await run_flow(client, "google")
    token, next_path = token_from(location)
    assert next_path == "/orders"

    user = await mock_mongo_db["users"].find_one({"google_id": "g-1"})
    assert user and user["email"] == "a@gmail.com" and user["role"] == "customer"
    assert "phone" not in user
    assert decode_token(token)["sub"] == user["id"]

    me = await client.get("/api/v1/auth/me", headers={"Authorization": f"Bearer {token}"})
    assert me.status_code == 200 and me.json()["data"]["phone"] is None

    # Đăng nhập lại: không tạo tài khoản mới
    await run_flow(client, "google")
    assert await mock_mongo_db["users"].count_documents({"google_id": "g-1"}) == 1


async def test_two_social_users_without_phone_coexist(client, mock_mongo_db, monkeypatch):
    fake_profile(monkeypatch, "google", "g-1", "a@gmail.com")
    await run_flow(client, "google")
    fake_profile(monkeypatch, "facebook", "f-2", None, "B")
    await run_flow(client, "facebook")
    assert await mock_mongo_db["users"].count_documents({}) == 2


async def test_links_existing_account_by_email(client, mock_mongo_db, monkeypatch):
    reg = await client.post("/api/v1/auth/register", json={
        "phone": "0912345678", "full_name": "Chị B", "password": "matkhau123", "email": "B@Gmail.com",
    })
    user_id = reg.json()["data"]["user_id"]

    fake_profile(monkeypatch, "facebook", "f-9", "b@gmail.com")
    token, _ = token_from(await run_flow(client, "facebook"))

    assert decode_token(token)["sub"] == user_id
    user = await mock_mongo_db["users"].find_one({"id": user_id})
    assert user["facebook_id"] == "f-9" and user["phone"] == "0912345678"
    assert await mock_mongo_db["users"].count_documents({}) == 1


async def test_callback_rejects_state_without_matching_cookie(client, monkeypatch):
    fake_profile(monkeypatch, "google", "g-1", "a@gmail.com")
    start = await client.get(f"{BASE}/google/start")
    state = parse_qs(urlparse(start.headers["location"]).query)["state"][0]
    client.cookies.clear()  # trình duyệt khác / kẻ tấn công gửi state của người khác
    cb = await client.get(f"{BASE}/google/callback", params={"code": "the-code", "state": state})
    assert cb.headers["location"].startswith("http://front.test/login?oauth_error=")


async def test_open_redirect_in_next_is_dropped(client, monkeypatch):
    fake_profile(monkeypatch, "google", "g-1", "a@gmail.com")
    _, next_path = token_from(await run_flow(client, "google", next_path="//evil.com"))
    assert next_path == ""


async def test_unconfigured_provider_redirects_with_error(client, monkeypatch):
    monkeypatch.setattr(settings, "FACEBOOK_APP_SECRET", "")
    res = await client.get(f"{BASE}/facebook/start")
    assert res.headers["location"].startswith("http://front.test/login?oauth_error=")


async def test_disabled_account_cannot_login(client, mock_mongo_db, monkeypatch):
    fake_profile(monkeypatch, "google", "g-1", "a@gmail.com")
    await run_flow(client, "google")
    await mock_mongo_db["users"].update_one({"google_id": "g-1"}, {"$set": {"is_active": False}})
    location = await run_flow(client, "google")
    assert "oauth_error=" in location
