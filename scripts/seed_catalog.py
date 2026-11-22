"""Nạp lại toàn bộ danh mục sản phẩm theo bảng giá võ phục & phụ kiện võ thuật.

Script XÓA SẠCH collection products / categories / martial_arts / size_charts rồi nạp lại,
đồng thời bỏ các dòng giỏ hàng trỏ tới sản phẩm không còn tồn tại. Đơn hàng cũ giữ nguyên.

Chạy từ thư mục tmdt_backend (đọc MONGODB_URI trong .env):
    python -m scripts.seed_catalog            # chỉ in ra những gì sẽ ghi
    python -m scripts.seed_catalog --apply    # ghi thật vào database
    python -m scripts.seed_catalog --apply --backup ../backup.json   # sao lưu dữ liệu cũ trước khi xóa
"""

import argparse
import asyncio
import json
from datetime import datetime, timedelta, timezone

import certifi
from motor.motor_asyncio import AsyncIOMotorClient

from app.core.config import settings

IMG = "/images/products"

MARTIAL_ARTS = [
    {"id": "vovinam", "name": "Vovinam (Việt Võ Đạo)", "description": "Võ thuật truyền thống Việt Nam, phát triển toàn diện đòn chân kẹp cổ và tấn pháp"},
    {"id": "taekwondo", "name": "Taekwondo", "description": "Môn võ Olympic với hệ thống đòn đá nhanh, mạnh và chuẩn xác"},
    {"id": "karate", "name": "Karate", "description": "Môn võ truyền thống Nhật Bản rèn luyện sự cương nhu và tinh thần võ sĩ đạo"},
    {"id": "vo_co_truyen", "name": "Võ cổ truyền", "description": "Di sản võ học ngàn năm của các môn phái truyền thống Việt Nam"},
    {"id": "chung", "name": "Dùng chung", "description": "Phụ kiện dùng được cho mọi môn võ"},
]

CATEGORIES = [
    {"id": "vo_phuc", "name": "Võ phục"},
    {"id": "dai", "name": "Đai võ thuật"},
    {"id": "bao_ho", "name": "Bảo hộ & giáp"},
    {"id": "tui_dung", "name": "Túi & balo"},
    {"id": "phu_kien", "name": "Phụ kiện tập luyện"},
]

# Bảng size võ phục Size 1 - Size 8 (áp dụng cho cả 4 môn)
SIZE_CHART = [
    ("Size 1", 105, 120, 16, 24, 4, 6, "Trẻ mẫu giáo 4-6 tuổi"),
    ("Size 2", 120, 130, 22, 30, 7, 8, "Trẻ tiểu học 7-8 tuổi"),
    ("Size 3", 130, 140, 28, 36, 9, 10, "Trẻ tiểu học 9-10 tuổi"),
    ("Size 4", 140, 150, 34, 44, 11, 13, "Học sinh 11-13 tuổi"),
    ("Size 5", 150, 160, 42, 52, 14, 16, "Học sinh cấp 2-3, người nhỏ gọn (S)"),
    ("Size 6", 160, 168, 50, 62, 16, 50, "Người lớn chuẩn (M)"),
    ("Size 7", 168, 176, 60, 74, 16, 50, "Người lớn cao đậm (L)"),
    ("Size 8", 176, 192, 72, 95, 16, 50, "Thể hình cao lớn (XL)"),
]

# Giá võ phục theo môn, Size 1 → Size 8 (VNĐ)
UNIFORMS = [
    {
        "art": "vovinam", "code": "VOV", "slug": "vo-phuc-vovinam", "name": "Võ phục Vovinam",
        "color": "Xanh dương", "material": "Vải kaki cotton",
        "prices": [170000, 175000, 185000, 190000, 195000, 200000, 205000, 215000],
        "images": ["vo-phuc-vovinam.jpg"],
        "description": "Võ phục Vovinam Việt Võ Đạo màu xanh dương truyền thống, may chắc chắn, thoáng mát khi tập luyện.",
    },
    {
        "art": "karate", "code": "KAR", "slug": "vo-phuc-karate", "name": "Võ phục Karate",
        "color": "Trắng", "material": "Vải kaki cotton",
        "prices": [175000, 180000, 190000, 195000, 200000, 205000, 210000, 220000],
        "images": ["vo-phuc-karate.jpg", "vo-phuc-karate-2.jpg"],
        "description": "Võ phục Karate trắng dáng chuẩn, phù hợp tập luyện kata và kumite.",
    },
    {
        "art": "taekwondo", "code": "TKD", "slug": "vo-phuc-taekwondo", "name": "Võ phục Taekwondo",
        "color": "Trắng", "material": "Vải kaki cotton",
        "prices": [170000, 175000, 185000, 190000, 195000, 200000, 205000, 215000],
        "images": ["vo-phuc-taekwondo.jpg"],
        "description": "Võ phục Taekwondo trắng cổ chữ V, co giãn tốt cho các đòn đá.",
    },
    {
        "art": "vo_co_truyen", "code": "VCT", "slug": "vo-phuc-vo-co-truyen", "name": "Võ phục Võ cổ truyền",
        "color": "Đen", "material": "Vải kaki cotton",
        "prices": [175000, 180000, 190000, 195000, 200000, 210000, 220000, 230000],
        "images": ["vo-phuc-vo-co-truyen.jpg"],
        "description": "Võ phục Võ cổ truyền Việt Nam màu đen, cúc bấm, bền và thoáng.",
    },
]

# Sản phẩm một cỡ: (mã, slug, tên, môn, loại, cấp đai, giá, màu, chất liệu, ảnh, mô tả)
ACCESSORIES = [
    # 2. Đai võ thuật
    ("DAI-VOV-LAM", "dai-vovinam-lam", "Đai Vovinam Lam", "vovinam", "dai", "lam_dai", 35000, "Xanh lam", "Vải kaki", ["dai-vovinam-lam.jpg"],
     "Đai Vovinam màu lam thêu chữ Vovinam, dành cho võ sinh cấp lam đai."),
    ("DAI-VOV-HOANG", "dai-vovinam-hoang", "Đai Vovinam Hoàng – Đai bản rộng 5cm", "vovinam", "dai", "hoang_dai", 45000, "Vàng", "Vải kaki", ["dai-vovinam-hoang.jpg"],
     "Đai Vovinam hoàng đai bản rộng 5cm, thêu chữ Vovinam Việt Võ Đạo."),
    ("DAI-TKD-DEN", "dai-taekwondo-den", "Đai Taekwondo Đai đen – Đẳng thêu chỉ vàng", "taekwondo", "dai", "dai_den", 80000, "Đen", "Vải kaki", ["dai-taekwondo-den.jpg"],
     "Đai đen Taekwondo thêu đẳng bằng chỉ vàng, dày dặn và giữ form."),
    ("DAI-KAR-TRANG", "dai-karate-trang", "Đai Karate Đai trắng – Vải bố 100% Cotton", "karate", "dai", "dai_trang", 35000, "Trắng", "Vải bố 100% cotton", ["dai-karate-trang.jpg"],
     "Đai trắng Karate may từ vải bố 100% cotton, dành cho võ sinh mới tập."),
    ("DAI-VCT-DO", "dai-co-truyen-do-dam", "Đai cổ truyền đỏ đậm – Vải sa tanh bản rộng", "vo_co_truyen", "dai", "dai_do", 40000, "Đỏ đậm", "Vải sa tanh", ["dai-co-truyen-do-dam.jpg"],
     "Đai Võ cổ truyền màu đỏ đậm, vải sa tanh bản rộng."),
    ("DAI-VCT-VANG", "dai-co-truyen-vang", "Đai cổ truyền vàng – Đai đẳng thắt lưng bụng", "vo_co_truyen", "dai", "dai_vang", 40000, "Vàng", "Vải sa tanh", ["dai-co-truyen-vang.jpg"],
     "Đai đẳng Võ cổ truyền màu vàng, kiểu thắt lưng bụng."),
    # 3. Bảo hộ & giáp
    ("BH-VOV-GIAP", "giap-thi-dau-vovinam", "Giáp thi đấu Vovinam – Bảo vệ ngực hai mặt", "vovinam", "bao_ho", None, 280000, "Đỏ/Xanh", "Da PU, mút đệm", ["giap-thi-dau-vovinam.jpg"],
     "Giáp thi đấu Vovinam hai mặt đỏ/xanh, bảo vệ ngực và bụng."),
    ("BH-TKD-MU", "mu-bao-ho-taekwondo", "Mũ bảo hộ thi đấu Taekwondo – Kính chắn", "taekwondo", "bao_ho", None, 300000, "Đỏ", "Mút EVA", ["mu-bao-ho-taekwondo.jpg"],
     "Mũ bảo hộ thi đấu Taekwondo có kính chắn mặt."),
    ("BH-TKD-TAYCHAN", "giap-tay-chan-taekwondo", "Giáp tay chân thi đấu Taekwondo – Chuẩn WT", "taekwondo", "bao_ho", None, 230000, "Xanh/Đỏ", "Mút EVA", ["giap-tay-chan-taekwondo.jpg"],
     "Bộ giáp tay chân thi đấu Taekwondo theo chuẩn WT."),
    ("BH-KAR-GANG", "gang-tay-karate-wkf", "Găng tay thi đấu Karate – WKF", "karate", "bao_ho", None, 160000, "Xanh/Đỏ", "Da PU, mút đệm", ["gang-tay-karate-wkf.jpg"],
     "Găng tay thi đấu Karate kiểu WKF, có màu xanh và đỏ."),
    ("BH-KAR-ONGDONG", "bao-ho-ong-dong-karate", "Bảo hộ ống đồng Muay – Bàn chân Karate", "karate", "bao_ho", None, 200000, "Đen", "Da PU, mút đệm", ["bao-ho-ong-dong-karate.jpg"],
     "Bảo hộ ống đồng kèm mu bàn chân, dùng cho Muay và Karate."),
    # 4. Túi & balo
    ("TUI-VOPHUC", "tui-dung-vo-phuc", "Túi đựng võ phục", "chung", "tui_dung", None, 160000, "Đen", "Vải dù chống thấm", ["tui-dung-vo-phuc.jpg"],
     "Túi đựng võ phục gọn nhẹ, chống thấm nước."),
    ("TUI-BALO", "balo-tap-luyen", "Balo tập luyện thể thao", "chung", "tui_dung", None, 180000, "Xanh dương", "Vải dù chống thấm", ["balo-tap-luyen.jpg"],
     "Balo tập luyện thể thao nhiều ngăn, đựng võ phục và phụ kiện."),
    ("TUI-THETHAO", "tui-the-thao-dung-cu", "Túi thể thao đựng dụng cụ võ thuật", "chung", "tui_dung", None, 100000, "Đen", "Vải dù", ["tui-the-thao-dung-cu.jpg"],
     "Túi trống thể thao đựng dụng cụ võ thuật, quai đeo vai."),
    # 5. Phụ kiện tập luyện
    ("PK-BANGQUAN", "bang-quan-tay", "Băng quấn tay", "chung", "phu_kien", None, 45000, "Trắng", "Vải cotton co giãn", ["bang-quan-tay.jpg"],
     "Băng quấn tay co giãn, bảo vệ khớp cổ tay và đốt ngón tay khi tập."),
    ("PK-CONNHI", "con-nhi-khuc-boc-mut", "Côn nhị khúc bọc mút mềm tập luyện an toàn", "vo_co_truyen", "phu_kien", None, 80000, "Đen", "Lõi nhựa bọc mút", ["con-nhi-khuc-boc-mut.jpg"],
     "Côn nhị khúc bọc mút mềm, an toàn cho người mới tập."),
]

STOCK_PER_VARIANT = 20


def build_products() -> list[dict]:
    base = datetime.now(timezone.utc)
    products: list[dict] = []

    def common(i: int) -> dict:
        # created_at giảm dần để "Mới nhất" giữ đúng thứ tự trong bảng giá
        ts = base - timedelta(minutes=i)
        return {
            "original_price": None,
            "discount_percent": 0,
            "rating": 0.0,
            "review_count": 0,
            "is_active": True,
            "created_at": ts,
            "updated_at": ts,
        }

    for u in UNIFORMS:
        variants = [
            {
                "sku": f"VP-{u['code']}-S{n}",
                "size": f"Size {n}",
                "color": u["color"],
                "price": price,
                "stock": STOCK_PER_VARIANT,
                "low_stock_threshold": 5,
            }
            for n, price in enumerate(u["prices"], start=1)
        ]
        products.append({
            "id": f"prod-vp-{u['code'].lower()}",
            "name": u["name"],
            "slug": u["slug"],
            "martial_art_id": u["art"],
            "category_id": "vo_phuc",
            "belt_rank": None,
            "price": min(u["prices"]),
            "thumbnail": f"{IMG}/{u['images'][0]}",
            "images": [f"{IMG}/{f}" for f in u["images"][1:]],
            "material": u["material"],
            "description": u["description"],
            "is_customizable": True,
            "variants": variants,
            **common(len(products)),
        })

    for code, slug, name, art, cat, belt, price, color, material, images, desc in ACCESSORIES:
        products.append({
            "id": f"prod-{code.lower()}",
            "name": name,
            "slug": slug,
            "martial_art_id": art,
            "category_id": cat,
            "belt_rank": belt,
            "price": price,
            "thumbnail": f"{IMG}/{images[0]}",
            "images": [f"{IMG}/{f}" for f in images[1:]],
            "material": material,
            "description": desc,
            "is_customizable": cat == "dai",
            "variants": [{
                "sku": code,
                "size": "Freesize",
                "color": color,
                "price": price,
                "stock": STOCK_PER_VARIANT,
                "low_stock_threshold": 5,
            }],
            **common(len(products)),
        })
    return products


def build_size_charts() -> list[dict]:
    return [
        {
            "id": f"chart-{art['id']}-{name.lower().replace(' ', '-')}",
            "martial_art_id": art["id"],
            "size_name": name,
            "height_min_cm": float(hmin), "height_max_cm": float(hmax),
            "weight_min_kg": float(wmin), "weight_max_kg": float(wmax),
            "age_min": amin, "age_max": amax,
            "fit_note": note,
        }
        for art in MARTIAL_ARTS if art["id"] != "chung"
        for name, hmin, hmax, wmin, wmax, amin, amax, note in SIZE_CHART
    ]


async def main(apply: bool, backup: str | None) -> None:
    uri = settings.MONGODB_URI
    tls = {"tlsCAFile": certifi.where()} if uri.startswith("mongodb+srv://") else {}
    client = AsyncIOMotorClient(uri, serverSelectionTimeoutMS=10000, **tls)
    db = client[settings.MONGODB_DB_NAME]

    products = build_products()
    charts = build_size_charts()
    print(f"DB: {settings.MONGODB_DB_NAME} @ {uri.split('@')[-1].split('/')[0]}")
    print(f"Sẽ nạp {len(products)} sản phẩm, {len(CATEGORIES)} loại, {len(MARTIAL_ARTS)} môn, {len(charts)} dòng bảng size")
    for p in products:
        sizes = ", ".join(f"{v['size']}={v['price']:,}" for v in p["variants"])
        print(f"  {p['id']:<22} {p['category_id']:<9} {p['martial_art_id']:<13} {p['name']}  [{sizes}]")

    if not apply:
        print("\nChưa ghi gì (thêm --apply để ghi thật).")
        return

    if backup:
        dump = {
            name: await db[name].find({}, {"_id": 0}).to_list(length=None)
            for name in ("products", "categories", "martial_arts", "size_charts")
        }
        with open(backup, "w", encoding="utf-8") as f:
            json.dump(dump, f, ensure_ascii=False, indent=1, default=str)
        print(f"Đã sao lưu dữ liệu cũ vào {backup}")

    for name, docs in (("products", products), ("categories", CATEGORIES), ("martial_arts", MARTIAL_ARTS), ("size_charts", charts)):
        deleted = (await db[name].delete_many({})).deleted_count
        await db[name].insert_many([dict(d) for d in docs])
        print(f"{name}: xóa {deleted}, nạp {len(docs)}")

    valid_ids = [p["id"] for p in products]
    res = await db["carts"].update_many({}, {"$pull": {"items": {"product_id": {"$nin": valid_ids}}}})
    print(f"carts: dọn dòng sản phẩm cũ ở {res.modified_count} giỏ")
    client.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--apply", action="store_true", help="Ghi thật vào database")
    parser.add_argument("--backup", help="Đường dẫn file JSON để sao lưu dữ liệu cũ trước khi xóa")
    args = parser.parse_args()
    asyncio.run(main(args.apply, args.backup))
