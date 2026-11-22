"""Đăng nhập bằng Google / Facebook (OAuth 2.0 Authorization Code, chạy hoàn toàn phía server).

Luồng:
1. Frontend chuyển trình duyệt tới  GET /auth/oauth/{provider}/start?next=/duong-dan
2. Backend đặt cookie chống CSRF (nonce) rồi chuyển hướng sang trang đồng ý của Google/Facebook.
3. Provider gọi lại      GET /auth/oauth/{provider}/callback?code=...&state=...
4. Backend đổi code lấy thông tin người dùng, tìm/liên kết/tạo tài khoản, cấp JWT như đăng nhập thường
   rồi chuyển về       {FRONTEND_URL}/auth/callback#token=...&next=...
   (token đặt trong fragment: không gửi lên server nào, không lọt vào log hay Referer).
Lỗi ở bất kỳ bước nào → {FRONTEND_URL}/login?oauth_error=<thông báo>.
"""
import hashlib
import hmac
import logging
import re
import secrets
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Optional
from urllib.parse import urlencode

import httpx
import jwt
from fastapi import APIRouter, Depends, Request
from fastapi.responses import RedirectResponse
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import settings
from app.core.database import get_db
from app.core.security import create_access_token, hash_password
from app.modules.users.models import UserDoc

logger = logging.getLogger("vo_ecommerce.auth.oauth")

router = APIRouter(prefix="/auth/oauth", tags=["Authentication & Accounts (F01)"])

PROVIDERS = ("google", "facebook")
STATE_COOKIE = "oauth_nonce"
STATE_TTL_SECONDS = 600
HTTP_TIMEOUT = 10.0


class OAuthError(Exception):
    """Lỗi hiển thị được cho người dùng (đưa vào ?oauth_error=)."""


@dataclass
class OAuthProfile:
    provider: str
    subject: str  # ID người dùng phía provider
    email: Optional[str]
    full_name: str


# ---------------------------------------------------------------------------
# Tiện ích
# ---------------------------------------------------------------------------

def _redirect_uri(provider: str) -> str:
    return f"{settings.BACKEND_URL.rstrip('/')}{settings.API_V1_STR}/auth/oauth/{provider}/callback"


def _frontend(path: str) -> str:
    return f"{settings.FRONTEND_URL.rstrip('/')}{path}"


def _safe_next(next_path: Optional[str]) -> str:
    # Chỉ nhận đường dẫn nội bộ để không thành open redirect
    if next_path and next_path.startswith("/") and not next_path.startswith("//") and "\\" not in next_path:
        return next_path[:500]
    return ""


def _error_redirect(message: str) -> RedirectResponse:
    resp = RedirectResponse(_frontend("/login?" + urlencode({"oauth_error": message})), status_code=302)
    resp.delete_cookie(STATE_COOKIE, path=f"{settings.API_V1_STR}/auth/oauth")
    return resp


def _is_configured(provider: str) -> bool:
    if provider == "google":
        return bool(settings.GOOGLE_CLIENT_ID and settings.GOOGLE_CLIENT_SECRET)
    return bool(settings.FACEBOOK_APP_ID and settings.FACEBOOK_APP_SECRET)


def _provider_label(provider: str) -> str:
    return "Google" if provider == "google" else "Facebook"


# ---------------------------------------------------------------------------
# Provider: Google
# ---------------------------------------------------------------------------

def _google_authorize_url(state: str) -> str:
    return "https://accounts.google.com/o/oauth2/v2/auth?" + urlencode({
        "client_id": settings.GOOGLE_CLIENT_ID,
        "redirect_uri": _redirect_uri("google"),
        "response_type": "code",
        "scope": "openid email profile",
        "state": state,
        "prompt": "select_account",
    })


async def _google_profile(code: str) -> OAuthProfile:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as http:
        token_res = await http.post("https://oauth2.googleapis.com/token", data={
            "code": code,
            "client_id": settings.GOOGLE_CLIENT_ID,
            "client_secret": settings.GOOGLE_CLIENT_SECRET,
            "redirect_uri": _redirect_uri("google"),
            "grant_type": "authorization_code",
        })
        if token_res.status_code != 200:
            logger.warning("Google token exchange lỗi %s: %s", token_res.status_code, token_res.text[:300])
            raise OAuthError("Không xác thực được với Google. Vui lòng thử lại.")
        access_token = token_res.json().get("access_token")

        info_res = await http.get(
            "https://openidconnect.googleapis.com/v1/userinfo",
            headers={"Authorization": f"Bearer {access_token}"},
        )
        if info_res.status_code != 200:
            raise OAuthError("Không lấy được thông tin tài khoản Google.")
        info = info_res.json()

    if not info.get("sub"):
        raise OAuthError("Không lấy được thông tin tài khoản Google.")
    # Chỉ tin email Google đã xác minh, nếu không có thể bị dùng để chiếm tài khoản cùng email
    email = info.get("email") if info.get("email_verified") else None
    return OAuthProfile(
        provider="google",
        subject=str(info["sub"]),
        email=email.lower() if email else None,
        full_name=(info.get("name") or (email or "").split("@")[0] or "Khách hàng Google").strip(),
    )


# ---------------------------------------------------------------------------
# Provider: Facebook
# ---------------------------------------------------------------------------

def _facebook_authorize_url(state: str) -> str:
    return f"https://www.facebook.com/{settings.FACEBOOK_GRAPH_VERSION}/dialog/oauth?" + urlencode({
        "client_id": settings.FACEBOOK_APP_ID,
        "redirect_uri": _redirect_uri("facebook"),
        "response_type": "code",
        "scope": "public_profile,email",
        "state": state,
    })


async def _facebook_profile(code: str) -> OAuthProfile:
    graph = f"https://graph.facebook.com/{settings.FACEBOOK_GRAPH_VERSION}"
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT) as http:
        token_res = await http.get(f"{graph}/oauth/access_token", params={
            "client_id": settings.FACEBOOK_APP_ID,
            "client_secret": settings.FACEBOOK_APP_SECRET,
            "redirect_uri": _redirect_uri("facebook"),
            "code": code,
        })
        if token_res.status_code != 200:
            logger.warning("Facebook token exchange lỗi %s: %s", token_res.status_code, token_res.text[:300])
            raise OAuthError("Không xác thực được với Facebook. Vui lòng thử lại.")
        access_token = token_res.json().get("access_token", "")

        # appsecret_proof: Graph API chỉ chấp nhận token kèm chữ ký bằng App Secret của chính app này
        proof = hmac.new(
            settings.FACEBOOK_APP_SECRET.encode(), access_token.encode(), hashlib.sha256
        ).hexdigest()
        me_res = await http.get(f"{graph}/me", params={
            "fields": "id,name,email",
            "access_token": access_token,
            "appsecret_proof": proof,
        })
        if me_res.status_code != 200:
            raise OAuthError("Không lấy được thông tin tài khoản Facebook.")
        info = me_res.json()

    if not info.get("id"):
        raise OAuthError("Không lấy được thông tin tài khoản Facebook.")
    # Facebook chỉ trả email đã xác nhận; người dùng có thể từ chối quyền email
    email = info.get("email")
    return OAuthProfile(
        provider="facebook",
        subject=str(info["id"]),
        email=email.lower() if email else None,
        full_name=(info.get("name") or "Khách hàng Facebook").strip(),
    )


# ---------------------------------------------------------------------------
# Tìm / liên kết / tạo tài khoản
# ---------------------------------------------------------------------------

async def _resolve_user(db: AsyncIOMotorDatabase, profile: OAuthProfile) -> dict:
    users_col = db["users"]
    id_field = f"{profile.provider}_id"
    now = datetime.now(timezone.utc)

    # 1. Đã từng đăng nhập bằng provider này
    user = await users_col.find_one({id_field: profile.subject})
    if user:
        return user

    # 2. Đã có tài khoản cùng email (đăng ký bằng SĐT + email, hoặc provider kia) → liên kết
    if profile.email:
        user = await users_col.find_one({"email": profile.email})
        if not user:
            # Email cũ có thể lưu khác hoa/thường
            user = await users_col.find_one(
                {"email": {"$regex": f"^{re.escape(profile.email)}$", "$options": "i"}}
            )
        if user:
            await users_col.update_one(
                {"_id": user["_id"]},
                {"$set": {id_field: profile.subject, "updated_at": now}},
            )
            user[id_field] = profile.subject
            return user

    # 3. Tạo tài khoản khách hàng mới (chưa có SĐT, mật khẩu ngẫu nhiên: muốn đăng nhập bằng
    #    mật khẩu thì dùng "Quên mật khẩu" với email này)
    new_user = UserDoc(
        full_name=profile.full_name[:100] or "Khách hàng",
        hashed_password=hash_password(secrets.token_urlsafe(32)),
        role="customer",
        email=profile.email,
        created_at=now,
        updated_at=now,
    )
    doc = new_user.model_dump(exclude={"phone", "google_id", "facebook_id"})
    doc[id_field] = profile.subject
    await users_col.insert_one(doc)
    return doc


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------

@router.get("/providers")
async def list_providers():
    """Danh sách provider đã cấu hình (frontend dùng để ẩn nút chưa bật)."""
    return {
        "success": True,
        "message": "OK",
        "data": {p: _is_configured(p) for p in PROVIDERS},
    }


@router.get("/{provider}/start")
async def oauth_start(provider: str, request: Request, next: Optional[str] = None):
    if provider not in PROVIDERS:
        return _error_redirect("Phương thức đăng nhập không được hỗ trợ.")
    if not _is_configured(provider):
        return _error_redirect(f"Đăng nhập bằng {_provider_label(provider)} chưa được bật.")

    nonce = secrets.token_urlsafe(24)
    state = jwt.encode(
        {
            "nonce": nonce,
            "provider": provider,
            "next": _safe_next(next),
            "exp": datetime.now(timezone.utc) + timedelta(seconds=STATE_TTL_SECONDS),
        },
        settings.SECRET_KEY,
        algorithm=settings.ALGORITHM,
    )
    url = _google_authorize_url(state) if provider == "google" else _facebook_authorize_url(state)
    resp = RedirectResponse(url, status_code=302)
    resp.set_cookie(
        STATE_COOKIE,
        nonce,
        max_age=STATE_TTL_SECONDS,
        httponly=True,
        secure=settings.BACKEND_URL.startswith("https://"),
        samesite="lax",
        path=f"{settings.API_V1_STR}/auth/oauth",
    )
    return resp


@router.get("/{provider}/callback")
async def oauth_callback(
    provider: str,
    request: Request,
    code: Optional[str] = None,
    state: Optional[str] = None,
    error: Optional[str] = None,
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    if provider not in PROVIDERS or not _is_configured(provider):
        return _error_redirect("Phương thức đăng nhập không được hỗ trợ.")
    label = _provider_label(provider)
    if error or not code:
        return _error_redirect(f"Bạn đã hủy đăng nhập bằng {label}.")

    # Chống CSRF / login CSRF: state phải do chính server ký và khớp nonce trong cookie của trình duyệt này
    try:
        payload = jwt.decode(state or "", settings.SECRET_KEY, algorithms=[settings.ALGORITHM])
    except jwt.PyJWTError:
        return _error_redirect("Phiên đăng nhập đã hết hạn. Vui lòng thử lại.")
    cookie_nonce = request.cookies.get(STATE_COOKIE) or ""
    if payload.get("provider") != provider or not hmac.compare_digest(
        str(payload.get("nonce", "")), cookie_nonce
    ):
        return _error_redirect("Phiên đăng nhập không hợp lệ. Vui lòng thử lại.")

    try:
        profile = await (_google_profile(code) if provider == "google" else _facebook_profile(code))
        user = await _resolve_user(db, profile)
    except OAuthError as exc:
        return _error_redirect(str(exc))
    except httpx.HTTPError as exc:
        logger.warning("OAuth %s lỗi mạng: %s", provider, exc)
        return _error_redirect(f"Không kết nối được {label}. Vui lòng thử lại.")

    if not user.get("is_active", True):
        return _error_redirect("Tài khoản đã bị vô hiệu hóa.")

    role = user.get("role", "customer")
    now = datetime.now(timezone.utc)
    if role in ("admin", "staff"):
        await db["activity_logs"].insert_one({
            "id": f"log-{uuid.uuid4().hex[:8]}",
            "actor": user.get("phone") or user.get("email") or user["id"],
            "actor_role": role,
            "action": "ADMIN_LOGIN",
            "target_type": "auth",
            "target_id": user["id"],
            "details": {"full_name": user.get("full_name"), "method": provider},
            "timestamp": now,
        })

    token = create_access_token(subject=user["id"], role=role)
    fragment = urlencode({"token": token, "next": payload.get("next") or ""})
    resp = RedirectResponse(_frontend(f"/auth/callback#{fragment}"), status_code=302)
    resp.delete_cookie(STATE_COOKIE, path=f"{settings.API_V1_STR}/auth/oauth")
    return resp
