"""Tài khoản khách hàng: xem/sửa thông tin cá nhân, địa chỉ giao hàng mặc định,
và gắn các đơn đặt khi chưa đăng nhập vào tài khoản."""
import re
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.core.database import get_db
from app.core.dependencies import get_current_user
from app.core.responses import APIResponse

router = APIRouter(prefix="/account", tags=["Tài khoản khách hàng"])

MAX_CLAIM = 20
VN_PHONE = re.compile(r"^0\d{9}$")


def _digits(value: Optional[str]) -> str:
    return "".join(ch for ch in (value or "") if ch.isdigit())


class DefaultAddress(BaseModel):
    recipient_name: str = Field("", max_length=100)
    phone: str = Field("", max_length=15)
    street_address: str = Field("", max_length=200)
    ward: str = Field("", max_length=100)
    district: str = Field("", max_length=100)
    city_province: str = Field("", max_length=100)


class AccountProfile(BaseModel):
    id: str
    full_name: str
    phone: Optional[str] = None
    email: Optional[str] = None
    role: str
    default_address: Optional[DefaultAddress] = None
    linked_google: bool = False
    linked_facebook: bool = False
    created_at: Optional[datetime] = None


class AccountUpdate(BaseModel):
    full_name: Optional[str] = Field(None, min_length=2, max_length=100)
    phone: Optional[str] = Field(None, max_length=15)
    default_address: Optional[DefaultAddress] = None


class ClaimItem(BaseModel):
    code: str = Field(..., max_length=40)
    phone: str = Field(..., max_length=20)


class ClaimRequest(BaseModel):
    orders: List[ClaimItem] = Field(default_factory=list, max_length=MAX_CLAIM)


def _profile(user: dict) -> AccountProfile:
    addr = user.get("default_address")
    return AccountProfile(
        id=user["id"],
        full_name=user.get("full_name", ""),
        phone=user.get("phone"),
        email=user.get("email"),
        role=user.get("role", "customer"),
        default_address=DefaultAddress(**addr) if isinstance(addr, dict) else None,
        linked_google=bool(user.get("google_id")),
        linked_facebook=bool(user.get("facebook_id")),
        created_at=user.get("created_at"),
    )


@router.get("/profile", response_model=APIResponse[AccountProfile])
async def get_profile(current_user: dict = Depends(get_current_user)):
    return APIResponse(data=_profile(current_user))


@router.patch("/profile", response_model=APIResponse[AccountProfile])
async def update_profile(
    payload: AccountUpdate,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Sửa họ tên, bổ sung/đổi SĐT (tài khoản Google/Facebook ban đầu chưa có), lưu địa chỉ giao hàng mặc định."""
    users_col = db["users"]
    updates: dict = {}
    unset: dict = {}

    if payload.full_name is not None:
        updates["full_name"] = payload.full_name.strip()

    if payload.phone is not None:
        phone = _digits(payload.phone)
        if phone == "":
            # Không cho xóa SĐT của tài khoản đăng ký bằng SĐT (đó là tên đăng nhập)
            if not (current_user.get("google_id") or current_user.get("facebook_id")):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Không thể xóa số điện thoại đăng nhập")
            unset["phone"] = ""
        elif not VN_PHONE.match(phone):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Số điện thoại gồm 10 số, bắt đầu bằng 0")
        elif phone != current_user.get("phone"):
            if await users_col.find_one({"phone": phone, "id": {"$ne": current_user["id"]}}, {"_id": 1}):
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Số điện thoại này đã được dùng cho tài khoản khác")
            updates["phone"] = phone

    if payload.default_address is not None:
        addr = payload.default_address.model_dump()
        addr = {k: v.strip() for k, v in addr.items()}
        if addr["phone"]:
            addr["phone"] = _digits(addr["phone"])
        updates["default_address"] = addr

    if updates or unset:
        updates["updated_at"] = datetime.now(timezone.utc)
        op: dict = {"$set": updates}
        if unset:
            op["$unset"] = unset
        await users_col.update_one({"id": current_user["id"]}, op)

    fresh = await users_col.find_one({"id": current_user["id"]})
    return APIResponse(message="Đã lưu thông tin tài khoản", data=_profile(fresh))


@router.post("/claim-orders", response_model=APIResponse[dict])
async def claim_orders(
    payload: ClaimRequest,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Gắn các đơn đặt khi chưa đăng nhập (mã đơn + SĐT lưu trên trình duyệt) vào tài khoản.

    Chỉ gắn khi SĐT khớp đơn và đơn chưa thuộc tài khoản nào: có mã đơn + SĐT là bằng chứng đã đặt đơn đó,
    nên không ai "nhận" được đơn của người khác chỉ bằng cách biết số điện thoại.
    """
    orders_col = db["orders"]
    claimed: List[str] = []
    for item in payload.orders:
        code = item.code.strip().upper()
        order = await orders_col.find_one({"order_code": code}, {"_id": 1, "user_id": 1, "customer_phone": 1})
        if not order or order.get("user_id"):
            continue
        if not _digits(item.phone) or _digits(item.phone) != _digits(order.get("customer_phone")):
            continue
        res = await orders_col.update_one(
            {"_id": order["_id"], "$or": [{"user_id": None}, {"user_id": {"$exists": False}}]},
            {"$set": {"user_id": current_user["id"]}},
        )
        if res.modified_count:
            claimed.append(code)
    return APIResponse(
        message=f"Đã lưu {len(claimed)} đơn hàng vào tài khoản" if claimed else "Không có đơn mới cần lưu",
        data={"claimed": claimed},
    )
