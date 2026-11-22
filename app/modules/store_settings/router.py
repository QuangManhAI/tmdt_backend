"""Thông tin cửa hàng hiển thị trên toàn website (địa chỉ, hotline, email...), admin sửa được."""

from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, Depends, Response
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, EmailStr, Field, field_validator

from app.core.activity import record_activity
from app.core.database import get_db
from app.core.dependencies import require_role
from app.core.responses import APIResponse

router = APIRouter(prefix="/settings", tags=["Store Settings"])

DOC_ID = "store"


class StoreSettings(BaseModel):
    store_name: str = Field("VÕ SPORT", min_length=2, max_length=80)
    tagline: str = Field("Võ phục và trang bị võ thuật", max_length=120)
    address: str = Field("Số 123 Võ Văn Tần, Phường Võ Thị Sáu, Quận 3, TP. Hồ Chí Minh", min_length=5, max_length=250)
    hotline: str = Field("1900 6868", min_length=6, max_length=20, description="Số hiển thị, có thể có dấu cách")
    support_hours: str = Field("8:00 - 21:00", max_length=60)
    email: EmailStr = "lienhe@vosport.vn"
    zalo: Optional[str] = Field(None, max_length=20, description="Số Zalo (bỏ trống nếu không dùng)")
    facebook_url: Optional[str] = Field(None, max_length=200)
    map_url: Optional[str] = Field(None, max_length=500, description="Link Google Maps của cửa hàng")

    @field_validator("hotline", "zalo")
    @classmethod
    def _phone(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None  # chỉ zalo được bỏ trống; hotline rỗng sẽ bị kiểm tra bắt buộc bên dưới
        v = v.strip()
        if not v.replace(" ", "").replace(".", "").lstrip("+").isdigit():
            raise ValueError("Chỉ được nhập số, dấu cách hoặc dấu chấm")
        return v

    @field_validator("hotline", mode="after")
    @classmethod
    def _hotline_required(cls, v: Optional[str]) -> str:
        if not v:
            raise ValueError("Hotline không được để trống")
        return v

    @field_validator("facebook_url", "map_url")
    @classmethod
    def _url(cls, v: Optional[str]) -> Optional[str]:
        if v is None or not v.strip():
            return None
        if not v.startswith(("https://", "http://")):
            raise ValueError("Link phải bắt đầu bằng https://")
        return v.strip()


async def load_settings(db: AsyncIOMotorDatabase) -> StoreSettings:
    doc = await db["site_settings"].find_one({"id": DOC_ID}, {"_id": 0, "id": 0, "updated_at": 0, "updated_by": 0})
    return StoreSettings(**(doc or {}))


@router.get("/store", response_model=APIResponse[StoreSettings])
async def get_store_settings(response: Response, db: AsyncIOMotorDatabase = Depends(get_db)):
    """Công khai: website đọc để hiện hotline, địa chỉ, email ở header / footer."""
    response.headers["Cache-Control"] = "public, max-age=60"
    return APIResponse(success=True, message="Thông tin cửa hàng", data=await load_settings(db))


@router.put("/store", response_model=APIResponse[StoreSettings])
async def update_store_settings(
    payload: StoreSettings,
    current_user: dict = Depends(require_role(["admin"])),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Chỉ admin: cập nhật thông tin cửa hàng, có ghi nhật ký thao tác."""
    before = await load_settings(db)
    data = payload.model_dump()
    await db["site_settings"].update_one(
        {"id": DOC_ID},
        {"$set": {**data, "updated_at": datetime.now(timezone.utc), "updated_by": current_user.get("sub")}},
        upsert=True,
    )
    changed = {k: {"old": getattr(before, k), "new": v} for k, v in data.items() if getattr(before, k) != v}
    if changed:
        await record_activity(
            db=db,
            actor=current_user.get("phone", current_user.get("sub", "admin")),
            actor_role=current_user.get("role", "admin"),
            action="UPDATE_STORE_SETTINGS",
            target_type="settings",
            target_id=DOC_ID,
            details=changed,
        )
    return APIResponse(success=True, message="Đã lưu thông tin cửa hàng", data=payload)
