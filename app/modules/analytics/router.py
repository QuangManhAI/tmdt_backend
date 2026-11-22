from datetime import date, timedelta
from typing import Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase

from app.core.database import get_db
from app.core.dependencies import require_role
from app.core.responses import APIResponse
from app.modules.analytics.schemas import AnalyticsDashboard
from app.modules.analytics.service import build_dashboard, today_vn

router = APIRouter(prefix="/analytics", tags=["Data Analysis (Admin)"])

MAX_RANGE_DAYS = 731


@router.get("/dashboard", response_model=APIResponse[AnalyticsDashboard])
async def get_analytics_dashboard(
    start: Optional[date] = Query(None, description="Ngày bắt đầu (giờ VN), mặc định 29 ngày trước"),
    end: Optional[date] = Query(None, description="Ngày kết thúc (giờ VN), mặc định hôm nay"),
    granularity: Optional[Literal["day", "week", "month"]] = Query(None, description="Gom theo ngày / tuần / tháng; bỏ trống để tự chọn"),
    scope: Literal["realized", "all"] = Query("realized", description="realized: chỉ đơn đã ghi nhận doanh thu; all: mọi đơn chưa hủy"),
    martial_art: Optional[str] = Query(None, description="vovinam, taekwondo, karate, vo_co_truyen, chung"),
    category: Optional[str] = Query(None, description="vo_phuc, dai, bao_ho, tui_dung, phu_kien"),
    channel: Optional[Literal["retail", "club"]] = Query(None, description="Kênh bán: lẻ hoặc võ đường"),
    payment_method: Optional[Literal["COD", "MOMO", "cod", "momo"]] = Query(None),
    current_user: dict = Depends(require_role(["admin"])),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Phân tích bán hàng cho trang quản trị: tổng quan có so sánh kỳ trước, chuỗi thời gian,
    nhóm sản phẩm, môn võ, từng sản phẩm, size, trạng thái, thanh toán, tỉnh thành và giờ đặt hàng."""
    end = end or today_vn()
    start = start or end - timedelta(days=29)
    if abs((end - start).days) > MAX_RANGE_DAYS:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Khoảng thời gian tối đa là 2 năm")

    data = await build_dashboard(
        db,
        start=start,
        end=end,
        granularity=granularity,
        scope=scope,
        martial_art=martial_art or None,
        category=category or None,
        channel=channel,
        payment_method=payment_method,
    )
    return APIResponse(success=True, message="Lấy dữ liệu phân tích thành công", data=data)
