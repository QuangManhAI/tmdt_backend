"""Tạo đơn hàng DEMO để thử trang Phân tích khi database chưa có đơn thật.

Mọi đơn demo có cờ is_demo=True và mã bắt đầu bằng "DEMO-", gỡ sạch bằng --remove.
Chạy từ thư mục tmdt_backend:
    python -m scripts.seed_demo_orders                                  # chỉ in thống kê, không ghi
    python -m scripts.seed_demo_orders --apply                          # thay đơn demo: 01/09/2026 -> hôm nay, ~45 đơn/ngày
    python -m scripts.seed_demo_orders --apply --start 2026-06-01 --per-day 80
    python -m scripts.seed_demo_orders --remove                         # xóa toàn bộ đơn demo
"""

import argparse
import asyncio
import random
import uuid
from datetime import date, datetime, timedelta, timezone

import certifi
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import settings

VN_TZ = timezone(timedelta(hours=7))
PROVINCES = [
    ("TP. Hồ Chí Minh", 30), ("Hà Nội", 24), ("Đà Nẵng", 9), ("Cần Thơ", 6), ("Hải Phòng", 6),
    ("Bình Dương", 6), ("Đồng Nai", 5), ("Khánh Hòa", 4), ("Thừa Thiên Huế", 4), ("Nghệ An", 3),
]
STATUSES = [("completed", 46), ("delivered", 14), ("shipping", 7), ("processing", 5), ("paid", 5),
            ("confirmed", 6), ("pending_payment", 4), ("cancelled", 9), ("returned", 2)]
SIZE_WEIGHTS = [4, 9, 14, 17, 18, 15, 9, 5]  # Size 1..8
HOUR_WEIGHTS = [1, 0, 0, 0, 0, 1, 2, 4, 6, 7, 7, 6, 5, 5, 5, 6, 7, 8, 10, 13, 14, 12, 7, 3]
CATEGORY_WEIGHT = {"vo_phuc": 10, "dai": 7, "bao_ho": 3, "tui_dung": 2, "phu_kien": 2}


def _pick(rng: random.Random, pairs):
    values, weights = zip(*pairs)
    return rng.choices(values, weights=weights)[0]


def build_orders(products: list[dict], days: int = 90, seed: int = 42, start: date | None = None, per_day: float = 7) -> list[dict]:
    rng = random.Random(seed)
    today = datetime.now(VN_TZ).date()
    if start is not None:
        days = max(0, (today - start).days)
    weights = [CATEGORY_WEIGHT.get(p.get("category_id"), 1) for p in products]
    # Tập khách cố định để có khách mua lại nhiều lần (khoảng 40% đơn là khách cũ)
    customers = [f"09{rng.randint(10_000_000, 99_999_999)}" for _ in range(max(50, int(per_day * days * 0.6)))]
    orders = []
    for back in range(days, -1, -1):
        day = today - timedelta(days=back)
        progress = (days - back) / days if days else 1
        # Cuối tuần, mùa tựu trường (tháng 9) và trước Tết dương (tháng 12) bán nhiều hơn; xu hướng tăng dần
        factor = 1.0 + (0.45 if day.weekday() >= 5 else 0) + (0.35 if day.month == 9 and day.day <= 15 else 0) + progress * 0.4
        if day.month == 11 and day.day in (11, 20):  # ngày 11/11 và 20/11 chạy khuyến mãi
            factor += 0.9
        mean = per_day * factor / 1.5
        for _ in range(max(0, int(rng.gauss(mean, mean * 0.22)))):
            hour = rng.choices(range(24), weights=HOUR_WEIGHTS)[0]
            local = datetime(day.year, day.month, day.day, hour, rng.randint(0, 59), tzinfo=VN_TZ)
            items = []
            for p in rng.choices(products, weights=weights, k=rng.choices([1, 2, 3], weights=[6, 3, 1])[0]):
                variants = p.get("variants") or [{"sku": p["id"], "size": "Freesize", "price": p.get("price", 0)}]
                if p.get("category_id") == "vo_phuc" and len(variants) == 8:
                    v = rng.choices(variants, weights=SIZE_WEIGHTS)[0]
                else:
                    v = rng.choice(variants)
                embroider = p.get("is_customizable") and rng.random() < 0.25
                items.append({
                    "product_id": p["id"], "sku": v.get("sku"), "product_name": p.get("name"), "size": v.get("size"),
                    "color": v.get("color", ""), "price": int(v.get("price", p.get("price", 0))),
                    "quantity": rng.choices([1, 2, 3], weights=[8, 2, 1])[0],
                    "is_embroidered": bool(embroider), "embroidery_name": "DEMO" if embroider else None,
                    "embroidery_fee": 30000 if embroider else 0,
                })
            club = rng.random() < 0.07
            if club:
                for it in items:
                    it["quantity"] *= rng.randint(8, 20)
            subtotal = sum(it["price"] * it["quantity"] for it in items)
            embroidery = sum(it["embroidery_fee"] * it["quantity"] for it in items)
            shipping = 0 if subtotal >= 500000 else 30000
            status = _pick(rng, STATUSES)
            if back <= 2 and status in ("completed", "delivered", "returned"):
                status = "shipping"
            method = "COD" if rng.random() < 0.62 else "MOMO"
            code = f"DEMO-{local.strftime('%Y%m%d')}-{uuid.uuid4().hex[:5].upper()}"
            orders.append({
                "id": f"ord-demo-{uuid.uuid4().hex[:10]}", "order_code": code, "is_demo": True,
                "user_id": None, "channel": "club" if club else "retail",
                "customer_name": "Khách demo", "customer_phone": rng.choice(customers[: max(50, int(len(customers) * (0.35 + 0.65 * progress)))]),
                "shipping_address": {"recipient_name": "Khách demo", "street_address": "Demo", "city_province": _pick(rng, PROVINCES)},
                "subtotal": subtotal, "shipping_fee": shipping, "discount_amount": 0, "voucher_code": None,
                "embroidery_total": embroidery, "total_amount": subtotal + embroidery + shipping,
                "payment_method": method, "payment_status": "paid" if status not in ("pending_payment", "confirmed", "cancelled") else "unpaid",
                "status": status, "items": items, "status_history": [],
                "created_at": local.astimezone(timezone.utc).replace(tzinfo=None),
                "updated_at": local.astimezone(timezone.utc).replace(tzinfo=None),
            })
    return orders


async def main(apply: bool, remove: bool, start: date, per_day: float) -> None:
    uri = settings.MONGODB_URI
    tls = {"tlsCAFile": certifi.where()} if uri.startswith("mongodb+srv://") else {}
    db = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=10000, **tls)[settings.MONGODB_DB_NAME]
    print(f"DB: {settings.MONGODB_DB_NAME} @ {uri.split('@')[-1].split('/')[0]}")
    if remove:
        res = await db["orders"].delete_many({"is_demo": True})
        print(f"Đã xóa {res.deleted_count} đơn demo")
        return
    products = await db["products"].find({"is_active": True}, {"_id": 0}).to_list(length=None)
    if not products:
        print("Chưa có sản phẩm, chạy scripts.seed_catalog trước.")
        return
    orders = build_orders(products, start=start, per_day=per_day)
    units = sum(it["quantity"] for o in orders for it in o["items"])
    print(f"Sẽ tạo {len(orders):,} đơn demo từ {start:%d/%m/%Y} đến hôm nay: {units:,} sản phẩm, "
          f"{len({o['customer_phone'] for o in orders}):,} khách, tổng {sum(o['total_amount'] for o in orders):,}₫")
    if not apply:
        print("Chưa ghi gì (thêm --apply để ghi thật, --remove để gỡ).")
        return
    removed = (await db["orders"].delete_many({"is_demo": True})).deleted_count
    if removed:
        print(f"Đã thay {removed:,} đơn demo cũ")
    for i in range(0, len(orders), 1000):
        await db["orders"].insert_many(orders[i:i + 1000])
    print(f"Đã ghi {len(orders):,} đơn demo.")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true")
    parser.add_argument("--remove", action="store_true")
    parser.add_argument("--start", type=date.fromisoformat, default=date(2026, 9, 1), help="Ngày bắt đầu (YYYY-MM-DD)")
    parser.add_argument("--per-day", type=float, default=45, help="Số đơn trung bình mỗi ngày")
    args = parser.parse_args()
    asyncio.run(main(args.apply, args.remove, args.start, args.per_day))
