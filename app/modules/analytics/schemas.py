from datetime import date
from typing import Dict, List, Literal, Optional

from pydantic import BaseModel

Granularity = Literal["day", "week", "month"]
Scope = Literal["realized", "all"]


class AnalyticsFilters(BaseModel):
    """Bộ lọc đã chuẩn hóa, trả lại cho FE để hiển thị đúng khoảng đang xem."""
    start: date
    end: date
    granularity: Granularity
    scope: Scope
    martial_art: Optional[str] = None
    category: Optional[str] = None
    channel: Optional[str] = None
    payment_method: Optional[str] = None


class Metric(BaseModel):
    value: float
    previous: float
    change_pct: Optional[float] = None  # None khi kỳ trước bằng 0


class Summary(BaseModel):
    revenue: Metric
    orders: Metric
    units: Metric
    avg_order_value: Metric
    customers: Metric
    cancel_rate: Metric  # phần trăm đơn hủy trên tổng đơn trong kỳ


class TimePoint(BaseModel):
    period: str  # ngày bắt đầu của kỳ, YYYY-MM-DD
    label: str
    revenue: int
    previous_revenue: int  # doanh thu cùng vị trí ở kỳ trước để so sánh
    orders: int
    units: int
    units_by_category: Dict[str, int]


class GroupRow(BaseModel):
    id: str
    name: str
    revenue: int
    units: int
    orders: int
    share: float  # tỷ trọng doanh thu, 0-100


class ProductRow(BaseModel):
    product_id: str
    name: str
    category_id: str
    martial_art_id: str
    price: int  # giá niêm yết thấp nhất
    avg_price: int  # giá bán bình quân thực tế
    units: int
    revenue: int
    orders: int
    stock: int
    sell_through: float  # % đã bán trên (đã bán + tồn kho)


class CountRow(BaseModel):
    id: str
    name: str
    count: int
    revenue: int = 0


class HeatCell(BaseModel):
    weekday: int  # 0 = Thứ 2 ... 6 = Chủ nhật
    hour: int
    orders: int


class AnalyticsDashboard(BaseModel):
    filters: AnalyticsFilters
    summary: Summary
    timeseries: List[TimePoint]
    by_category: List[GroupRow]
    by_martial_art: List[GroupRow]
    products: List[ProductRow]
    sizes: List[CountRow]
    statuses: List[CountRow]
    payment_methods: List[CountRow]
    provinces: List[CountRow]
    heatmap: List[HeatCell]
