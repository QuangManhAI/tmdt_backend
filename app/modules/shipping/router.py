"""API vận chuyển: danh sách phường/xã gợi ý cho checkout và webhook trạng thái đơn của GHN."""

import logging
from typing import List

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.config import settings
from app.core.database import get_db
from app.core.responses import APIResponse
from app.modules.shipping.ghn import GHNError
from app.modules.shipping.provider import GHNShippingProvider, shipping_service
from app.modules.shipping.tracking import apply_shipment_status

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/shipping", tags=["Shipping"])


@router.get("/wards", response_model=APIResponse[List[str]])
async def list_wards(response: Response, province: str = Query(..., min_length=2)):
    """Tên phường/xã (địa giới mới) của một tỉnh theo danh mục GHN, để khách chọn đúng tên khi nhập địa chỉ.
    Chưa bật GHN hoặc GHN lỗi thì trả danh sách rỗng (khách vẫn tự gõ được)."""
    if not isinstance(shipping_service, GHNShippingProvider):
        return APIResponse(success=True, message="Chưa bật đơn vị vận chuyển", data=[])
    try:
        p = await shipping_service.client.find_province(province)
        wards = await shipping_service.client.wards(int(p["_id"])) if p else []
    except GHNError:
        return APIResponse(success=True, message="Tạm thời chưa tải được danh sách phường, xã", data=[])
    response.headers["Cache-Control"] = "public, max-age=86400"
    return APIResponse(success=True, message="Danh sách phường, xã", data=sorted(w["name"] for w in wards))


@router.post("/ghn/webhook")
async def ghn_webhook(request: Request, db: AsyncIOMotorDatabase = Depends(get_db)):
    """GHN gọi khi vận đơn đổi trạng thái. GHN không ký payload nên KHÔNG tin trạng thái trong body:
    chỉ dùng mã vận đơn để hỏi lại GHN bằng token của shop rồi mới cập nhật.
    Trả 2xx cho sự kiện bỏ qua (để GHN khỏi gửi lại), 5xx khi cần GHN gửi lại sau."""
    try:
        payload = await request.json()
    except ValueError:
        return {"ok": False, "reason": "invalid_json"}
    if not isinstance(payload, dict) or not isinstance(shipping_service, GHNShippingProvider):
        return {"ok": False, "reason": "ignored"}

    if str(payload.get("ShopID", "")) != str(settings.GHN_SHOP_ID).strip():
        return {"ok": False, "reason": "other_shop"}
    tracking_code = str(payload.get("OrderCode") or "")
    if not tracking_code:
        return {"ok": False, "reason": "missing_order_code"}

    order = await db["orders"].find_one({"tracking_code": tracking_code, "carrier": shipping_service.name})
    if not order:
        return {"ok": False, "reason": "unknown_order"}

    info = await shipping_service.get_status(tracking_code)
    if not info:
        # Không xác minh được với GHN: báo lỗi để GHN gửi lại sau
        raise HTTPException(status_code=503, detail="Chưa xác minh được trạng thái với GHN")

    await apply_shipment_status(db, order, info, source="webhook")
    return {"ok": True, "status": info["status"]}
