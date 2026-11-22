"""Tính số liệu phân tích bán hàng từ collection orders + products.

Mọi mốc thời gian được quy về giờ Việt Nam (UTC+7) trước khi gom nhóm, để "hôm nay"
và biểu đồ theo giờ khớp với cảm nhận của chủ cửa hàng.
"""

from collections import defaultdict
from datetime import date, datetime, time, timedelta, timezone
from typing import Dict, Iterable, List, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.modules.analytics.schemas import (
    AnalyticsDashboard,
    AnalyticsFilters,
    CountRow,
    GroupRow,
    HeatCell,
    Metric,
    ProductRow,
    Summary,
    TimePoint,
)

VN_TZ = timezone(timedelta(hours=7))

# Đơn đã ghi nhận doanh thu (đồng bộ với báo cáo F22)
REALIZED_STATUSES = {"paid", "processing", "shipping", "delivered", "completed"}
CANCELLED = "cancelled"

UNKNOWN = "khac"
CATEGORY_NAMES = {
    "vo_phuc": "Võ phục",
    "dai": "Đai võ thuật",
    "bao_ho": "Bảo hộ & giáp",
    "tui_dung": "Túi & balo",
    "phu_kien": "Phụ kiện tập luyện",
    UNKNOWN: "Khác",
}
MARTIAL_ART_NAMES = {
    "vovinam": "Vovinam",
    "taekwondo": "Taekwondo",
    "karate": "Karate",
    "vo_co_truyen": "Võ cổ truyền",
    "chung": "Dùng chung",
    UNKNOWN: "Khác",
}
STATUS_NAMES = {
    "pending_payment": "Chờ thanh toán",
    "confirmed": "Đã xác nhận",
    "paid": "Đã thanh toán",
    "processing": "Đang chuẩn bị",
    "shipping": "Đang giao",
    "delivered": "Đã giao",
    "completed": "Hoàn tất",
    "return_requested": "Yêu cầu đổi size",
    "returned": "Đã đổi trả",
    "cancelled": "Đã hủy",
}
PAYMENT_NAMES = {"COD": "Thanh toán khi nhận", "MOMO": "Ví MoMo"}

ORDER_PROJECTION = {
    "_id": 0,
    "created_at": 1,
    "status": 1,
    "channel": 1,
    "payment_method": 1,
    "customer_phone": 1,
    "user_id": 1,
    "items": 1,
    "shipping_address.city_province": 1,
}


def today_vn() -> date:
    return datetime.now(VN_TZ).date()


def auto_granularity(start: date, end: date) -> str:
    days = (end - start).days + 1
    if days <= 45:
        return "day"
    if days <= 200:
        return "week"
    return "month"


def _to_local(dt: datetime) -> datetime:
    # pymongo trả datetime không kèm múi giờ nhưng giá trị là UTC
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(VN_TZ)


def _bucket_start(d: date, granularity: str) -> date:
    if granularity == "week":
        return d - timedelta(days=d.weekday())
    if granularity == "month":
        return d.replace(day=1)
    return d


def _next_bucket(d: date, granularity: str) -> date:
    if granularity == "week":
        return d + timedelta(days=7)
    if granularity == "month":
        return (d.replace(day=28) + timedelta(days=4)).replace(day=1)
    return d + timedelta(days=1)


def _bucket_label(d: date, granularity: str) -> str:
    if granularity == "week":
        return f"Tuần {d.strftime('%d/%m')}"
    if granularity == "month":
        return d.strftime("%m/%Y")
    return d.strftime("%d/%m")


def _metric(current: float, previous: float) -> Metric:
    change = None if previous == 0 else round((current - previous) / previous * 100, 1)
    return Metric(value=round(current, 2), previous=round(previous, 2), change_pct=change)


def _line_amount(item: dict) -> int:
    qty = int(item.get("quantity", 1) or 1)
    return (int(item.get("price", 0) or 0) + int(item.get("embroidery_fee", 0) or 0)) * qty


class _Period:
    """Tổng hợp cho một kỳ (kỳ hiện tại hoặc kỳ trước)."""

    def __init__(self) -> None:
        self.revenue = 0
        self.units = 0
        self.orders = 0
        self.customers: set = set()
        self.all_orders = 0
        self.cancelled = 0

    def summary_values(self) -> Dict[str, float]:
        return {
            "revenue": self.revenue,
            "orders": self.orders,
            "units": self.units,
            "aov": self.revenue / self.orders if self.orders else 0,
            "customers": len(self.customers),
            "cancel_rate": self.cancelled / self.all_orders * 100 if self.all_orders else 0,
        }


async def build_dashboard(
    db: AsyncIOMotorDatabase,
    start: date,
    end: date,
    granularity: Optional[str] = None,
    scope: str = "realized",
    martial_art: Optional[str] = None,
    category: Optional[str] = None,
    channel: Optional[str] = None,
    payment_method: Optional[str] = None,
) -> AnalyticsDashboard:
    if end < start:
        start, end = end, start
    granularity = granularity or auto_granularity(start, end)
    period_days = (end - start).days + 1
    prev_start = start - timedelta(days=period_days)

    utc_from = datetime.combine(prev_start, time.min, VN_TZ).astimezone(timezone.utc)
    utc_to = datetime.combine(end + timedelta(days=1), time.min, VN_TZ).astimezone(timezone.utc)

    # Sản phẩm hiện có: dùng để biết loại / môn của từng dòng hàng và tồn kho
    product_docs = await db["products"].find(
        {}, {"_id": 0, "id": 1, "name": 1, "category_id": 1, "martial_art_id": 1, "price": 1, "variants": 1}
    ).to_list(length=None)
    products = {p["id"]: p for p in product_docs if p.get("id")}

    order_query: dict = {"created_at": {"$gte": utc_from.replace(tzinfo=None), "$lt": utc_to.replace(tzinfo=None)}}
    if channel:
        order_query["channel"] = channel if channel == "club" else {"$ne": "club"}
    if payment_method:
        order_query["payment_method"] = payment_method.upper()
    orders = await db["orders"].find(order_query, ORDER_PROJECTION).to_list(length=50000)

    def counts_for_revenue(status: str) -> bool:
        if scope == "all":
            return status != CANCELLED
        return status in REALIZED_STATUSES

    def item_matches(item: dict) -> bool:
        p = products.get(item.get("product_id"), {})
        if category and p.get("category_id", UNKNOWN) != category:
            return False
        if martial_art and p.get("martial_art_id", UNKNOWN) != martial_art:
            return False
        return True

    # Khung thời gian đầy đủ (kể cả kỳ không có đơn) để đường biểu đồ liền mạch
    buckets: List[date] = []
    cursor = _bucket_start(start, granularity)
    while cursor <= end:
        buckets.append(cursor)
        cursor = _next_bucket(cursor, granularity)
    series = {
        b: {"revenue": 0, "previous_revenue": 0, "orders": 0, "units": 0, "units_by_category": defaultdict(int)}
        for b in buckets
    }

    current, previous = _Period(), _Period()
    by_category: Dict[str, Dict] = defaultdict(lambda: {"revenue": 0, "units": 0, "orders": set()})
    by_art: Dict[str, Dict] = defaultdict(lambda: {"revenue": 0, "units": 0, "orders": set()})
    by_product: Dict[str, Dict] = defaultdict(lambda: {"revenue": 0, "units": 0, "orders": set(), "name": ""})
    sizes: Dict[str, Dict] = defaultdict(lambda: {"count": 0, "revenue": 0})
    statuses: Dict[str, Dict] = defaultdict(lambda: {"count": 0, "revenue": 0})
    payments: Dict[str, Dict] = defaultdict(lambda: {"count": 0, "revenue": 0})
    provinces: Dict[str, Dict] = defaultdict(lambda: {"count": 0, "revenue": 0})
    heat: Dict[tuple, int] = defaultdict(int)

    for idx, o in enumerate(orders):
        created = o.get("created_at")
        if not isinstance(created, datetime):
            continue
        local = _to_local(created)
        local_day = local.date()
        is_current = local_day >= start
        period = current if is_current else previous

        items = [it for it in o.get("items", []) if item_matches(it)]
        if not items:
            continue
        status = o.get("status", "pending_payment")
        amount = sum(_line_amount(it) for it in items)
        units = sum(int(it.get("quantity", 1) or 1) for it in items)

        period.all_orders += 1
        if status == CANCELLED:
            period.cancelled += 1
        if is_current:
            statuses[status]["count"] += 1
            statuses[status]["revenue"] += amount

        if not counts_for_revenue(status):
            continue

        period.revenue += amount
        period.units += units
        period.orders += 1
        period.customers.add(o.get("customer_phone") or o.get("user_id") or f"guest-{idx}")

        if not is_current:
            # Dịch đơn kỳ trước sang đúng vị trí tương ứng trong kỳ hiện tại để so sánh
            shifted = _bucket_start(local_day + timedelta(days=period_days), granularity)
            if shifted in series:
                series[shifted]["previous_revenue"] += amount
            continue

        point = series.get(_bucket_start(local_day, granularity))
        if point is not None:
            point["revenue"] += amount
            point["orders"] += 1
            point["units"] += units

        method = (o.get("payment_method") or "COD").upper()
        payments[method]["count"] += 1
        payments[method]["revenue"] += amount
        province = ((o.get("shipping_address") or {}).get("city_province") or "Không rõ").strip()
        provinces[province]["count"] += 1
        provinces[province]["revenue"] += amount
        heat[(local.weekday(), local.hour)] += 1

        order_key = idx
        for it in items:
            pid = it.get("product_id") or it.get("sku") or UNKNOWN
            p = products.get(pid, {})
            cat = p.get("category_id", UNKNOWN)
            art = p.get("martial_art_id", UNKNOWN)
            line = _line_amount(it)
            qty = int(it.get("quantity", 1) or 1)
            for bucket, key in ((by_category, cat), (by_art, art)):
                bucket[key]["revenue"] += line
                bucket[key]["units"] += qty
                bucket[key]["orders"].add(order_key)
            row = by_product[pid]
            row["revenue"] += line
            row["units"] += qty
            row["orders"].add(order_key)
            row["name"] = row["name"] or it.get("product_name") or p.get("name", pid)
            if point is not None:
                point["units_by_category"][cat] += qty
            if cat == "vo_phuc" and it.get("size"):
                sizes[it["size"]]["count"] += qty
                sizes[it["size"]]["revenue"] += line

    cur, prev = current.summary_values(), previous.summary_values()
    summary = Summary(
        revenue=_metric(cur["revenue"], prev["revenue"]),
        orders=_metric(cur["orders"], prev["orders"]),
        units=_metric(cur["units"], prev["units"]),
        avg_order_value=_metric(cur["aov"], prev["aov"]),
        customers=_metric(cur["customers"], prev["customers"]),
        cancel_rate=_metric(cur["cancel_rate"], prev["cancel_rate"]),
    )

    timeseries = [
        TimePoint(
            period=b.isoformat(),
            label=_bucket_label(b, granularity),
            revenue=v["revenue"],
            previous_revenue=v["previous_revenue"],
            orders=v["orders"],
            units=v["units"],
            units_by_category=dict(v["units_by_category"]),
        )
        for b, v in series.items()
    ]

    def group_rows(data: Dict[str, Dict], names: Dict[str, str], include: Iterable[str]) -> List[GroupRow]:
        total = sum(v["revenue"] for v in data.values()) or 1
        keys = list(dict.fromkeys([*include, *data.keys()]))
        rows = [
            GroupRow(
                id=k,
                name=names.get(k, k),
                revenue=data[k]["revenue"] if k in data else 0,
                units=data[k]["units"] if k in data else 0,
                orders=len(data[k]["orders"]) if k in data else 0,
                share=round((data[k]["revenue"] if k in data else 0) / total * 100, 1),
            )
            for k in keys
        ]
        # Nhóm "Khác" chỉ hiện khi có số liệu
        rows = [r for r in rows if r.id != UNKNOWN or r.revenue > 0]
        return sorted(rows, key=lambda r: r.revenue, reverse=True)

    cat_ids = [category] if category else [k for k in CATEGORY_NAMES if k != UNKNOWN]
    art_ids = [martial_art] if martial_art else [k for k in MARTIAL_ART_NAMES if k != UNKNOWN]

    product_rows: List[ProductRow] = []
    for pid, p in products.items():
        if category and p.get("category_id") != category:
            continue
        if martial_art and p.get("martial_art_id") != martial_art:
            continue
        sold = by_product.get(pid)
        units = sold["units"] if sold else 0
        revenue = sold["revenue"] if sold else 0
        stock = sum(int(v.get("stock", 0) or 0) for v in p.get("variants", []))
        product_rows.append(ProductRow(
            product_id=pid,
            name=p.get("name", pid),
            category_id=p.get("category_id", UNKNOWN),
            martial_art_id=p.get("martial_art_id", UNKNOWN),
            price=int(p.get("price", 0) or 0),
            avg_price=round(revenue / units) if units else int(p.get("price", 0) or 0),
            units=units,
            revenue=revenue,
            orders=len(sold["orders"]) if sold else 0,
            stock=stock,
            sell_through=round(units / (units + stock) * 100, 1) if units + stock else 0,
        ))
    # Sản phẩm đã ngừng bán nhưng vẫn có doanh thu trong kỳ
    for pid, sold in by_product.items():
        if pid not in products:
            product_rows.append(ProductRow(
                product_id=pid, name=sold["name"] or pid, category_id=UNKNOWN, martial_art_id=UNKNOWN,
                price=0, avg_price=round(sold["revenue"] / sold["units"]) if sold["units"] else 0,
                units=sold["units"], revenue=sold["revenue"], orders=len(sold["orders"]), stock=0, sell_through=100.0,
            ))
    product_rows.sort(key=lambda r: (r.revenue, r.units), reverse=True)

    def count_rows(data: Dict[str, Dict], names: Dict[str, str], order: Optional[List[str]] = None) -> List[CountRow]:
        rows = [CountRow(id=k, name=names.get(k, k), count=v["count"], revenue=v["revenue"]) for k, v in data.items()]
        if order:
            rank = {k: i for i, k in enumerate(order)}
            return sorted(rows, key=lambda r: rank.get(r.id, len(rank)))
        return sorted(rows, key=lambda r: r.revenue, reverse=True)

    size_order = [f"Size {n}" for n in range(1, 9)]

    return AnalyticsDashboard(
        filters=AnalyticsFilters(
            start=start, end=end, granularity=granularity, scope=scope,
            martial_art=martial_art, category=category, channel=channel, payment_method=payment_method,
        ),
        summary=summary,
        timeseries=timeseries,
        by_category=group_rows(by_category, CATEGORY_NAMES, cat_ids),
        by_martial_art=group_rows(by_art, MARTIAL_ART_NAMES, art_ids),
        products=product_rows,
        sizes=count_rows(sizes, {}, size_order),
        statuses=count_rows(statuses, STATUS_NAMES, list(STATUS_NAMES)),
        payment_methods=count_rows(payments, PAYMENT_NAMES),
        provinces=count_rows(provinces, {})[:10],
        heatmap=[HeatCell(weekday=w, hour=h, orders=c) for (w, h), c in sorted(heat.items())],
    )
