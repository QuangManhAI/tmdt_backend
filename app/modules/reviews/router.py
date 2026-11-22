"""Đánh giá sản phẩm.

- Chỉ khách đã đăng nhập và đã NHẬN HÀNG sản phẩm đó (đơn của tài khoản ở trạng thái delivered/completed)
  mới viết được; đánh giá được gắn nhãn "Đã mua hàng".
- Mỗi tài khoản 1 đánh giá / sản phẩm; gửi lại là sửa đánh giá cũ.
- Đánh giá hiện ngay; nhân viên/quản trị viên ẩn/hiện được (is_hidden).
"""
import uuid
from datetime import datetime, timezone
from typing import List, Literal, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pydantic import BaseModel, Field

from app.core.activity import record_activity
from app.core.database import get_db
from app.core.dependencies import get_current_user, require_role
from app.core.responses import APIResponse

router = APIRouter(prefix="/reviews", tags=["Product Reviews & Sizing Feedback"])

REVIEWABLE_STATUSES = ["delivered", "completed", "return_requested", "returned"]
STAFF_ROLES = ["admin", "staff"]


class ReviewCreate(BaseModel):
    product_id: str
    rating: int = Field(5, ge=1, le=5)
    comment: str = Field(..., min_length=1, max_length=2000)
    size_fit: Literal["tight", "fit", "loose"] = Field("fit", description="tight (hơi chật), fit (vừa vặn), loose (hơi rộng)")
    # Bỏ trống thì lấy size trong đơn đã mua
    bought_size: Optional[str] = Field(None, max_length=20)


class ReviewVisibility(BaseModel):
    hidden: bool


def _utc(value):
    if isinstance(value, datetime) and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _public(r: dict) -> dict:
    return {
        "id": r.get("id", str(r.get("_id", ""))),
        "product_id": r.get("product_id", ""),
        "user_name": r.get("user_name", ""),
        "rating": r.get("rating", 5),
        "comment": r.get("comment", ""),
        "size_fit": r.get("size_fit", "fit"),
        "bought_size": r.get("bought_size", ""),
        "verified_purchase": bool(r.get("verified_purchase")),
        "created_at": _utc(r.get("created_at")),
        "updated_at": _utc(r.get("updated_at")),
    }


async def _purchased_items(db, user_id: str, product_id: Optional[str] = None) -> List[dict]:
    """Các sản phẩm khách đã nhận hàng, mỗi sản phẩm 1 dòng (đơn giao gần nhất)."""
    query: dict = {"user_id": user_id, "status": {"$in": REVIEWABLE_STATUSES}}
    if product_id:
        query["items.product_id"] = product_id
    cursor = db["orders"].find(query, {"order_code": 1, "items": 1, "updated_at": 1, "created_at": 1}).sort("created_at", -1)
    seen: dict = {}
    async for order in cursor:
        for it in order.get("items", []):
            pid = it.get("product_id")
            if not pid or pid in seen or (product_id and pid != product_id):
                continue
            seen[pid] = {
                "product_id": pid,
                "product_name": it.get("product_name", ""),
                "size": it.get("size", ""),
                "order_code": order.get("order_code"),
                "delivered_at": _utc(order.get("updated_at") or order.get("created_at")),
            }
    return list(seen.values())


@router.get("")
async def list_reviews(product_id: Optional[str] = None, db: AsyncIOMotorDatabase = Depends(get_db)):
    """Đánh giá công khai của sản phẩm (không gồm đánh giá đã bị ẩn)."""
    query: dict = {"is_hidden": {"$ne": True}}
    if product_id:
        query["product_id"] = product_id
    items = await db["reviews"].find(query).sort("created_at", -1).to_list(length=100)
    return APIResponse(success=True, message="Lấy danh sách đánh giá thành công", data=[_public(r) for r in items])


@router.get("/me")
async def my_reviewable_products(
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Sản phẩm khách đã nhận hàng kèm đánh giá của khách (null nếu chưa viết)."""
    purchased = await _purchased_items(db, current_user["id"])
    mine = {r["product_id"]: r async for r in db["reviews"].find({"user_id": current_user["id"]})}
    products = {
        p["id"]: p
        async for p in db["products"].find(
            {"id": {"$in": [p["product_id"] for p in purchased]}}, {"id": 1, "slug": 1, "images": 1}
        )
    }
    data = []
    for item in purchased:
        prod = products.get(item["product_id"], {})
        images = prod.get("images") or []
        review = mine.get(item["product_id"])
        data.append({
            **item,
            "product_slug": prod.get("slug"),
            "image": images[0] if images and isinstance(images[0], str) else None,
            "review": {**_public(review), "is_hidden": bool(review.get("is_hidden"))} if review else None,
        })
    return APIResponse(success=True, message="OK", data=data)


@router.post("")
async def create_or_update_review(
    req: ReviewCreate,
    current_user: dict = Depends(get_current_user),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Viết (hoặc sửa) đánh giá. Bắt buộc đã nhận hàng sản phẩm này; tên lấy từ tài khoản để chống giả mạo."""
    if not await db["products"].find_one({"id": req.product_id}, {"_id": 1}):
        raise HTTPException(status_code=404, detail="Sản phẩm không tồn tại")
    purchased = await _purchased_items(db, current_user["id"], req.product_id)
    if not purchased:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Chỉ khách đã mua và nhận sản phẩm này mới viết được đánh giá",
        )
    item = purchased[0]
    comment = req.comment.strip()
    if not comment:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Vui lòng nhập nội dung đánh giá")

    now = datetime.now(timezone.utc)
    reviews_col = db["reviews"]
    fields = {
        "user_name": current_user.get("full_name") or "Khách hàng",
        "rating": req.rating,
        "comment": comment,
        "size_fit": req.size_fit,
        "bought_size": (req.bought_size or item["size"] or "").strip(),
        "verified_purchase": True,
        "order_code": item["order_code"],
        "updated_at": now,
    }
    existing = await reviews_col.find_one({"user_id": current_user["id"], "product_id": req.product_id})
    if existing:
        await reviews_col.update_one({"_id": existing["_id"]}, {"$set": fields})
        doc = {**existing, **fields}
        message = "Đã cập nhật đánh giá"
    else:
        doc = {
            "id": f"rev-{uuid.uuid4().hex[:8]}",
            "product_id": req.product_id,
            "user_id": current_user["id"],
            "is_hidden": False,
            "created_at": now,
            **fields,
        }
        await reviews_col.insert_one(doc)
        message = "Cảm ơn bạn đã đánh giá"
    return APIResponse(success=True, message=message, data=_public(doc))


# ---------------------------------------------------------------------------
# Quản trị
# ---------------------------------------------------------------------------

@router.get("/admin/list")
async def admin_list_reviews(
    visibility: str = Query("all", pattern="^(all|visible|hidden)$"),
    limit: int = Query(100, ge=1, le=300),
    _: dict = Depends(require_role(STAFF_ROLES)),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    query: dict = {}
    if visibility == "hidden":
        query["is_hidden"] = True
    elif visibility == "visible":
        query["is_hidden"] = {"$ne": True}
    items = await db["reviews"].find(query).sort("created_at", -1).to_list(length=limit)
    names = {
        p["id"]: p.get("name", "")
        async for p in db["products"].find({"id": {"$in": list({r.get("product_id") for r in items})}}, {"id": 1, "name": 1})
    }
    data = [
        {**_public(r), "is_hidden": bool(r.get("is_hidden")), "product_name": names.get(r.get("product_id"), ""), "order_code": r.get("order_code")}
        for r in items
    ]
    return APIResponse(success=True, message="OK", data=data)


@router.patch("/{review_id}/visibility")
async def set_review_visibility(
    review_id: str,
    payload: ReviewVisibility,
    current_user: dict = Depends(require_role(STAFF_ROLES)),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    review = await db["reviews"].find_one({"id": review_id})
    if not review:
        raise HTTPException(status_code=404, detail="Không tìm thấy đánh giá")
    await db["reviews"].update_one({"id": review_id}, {"$set": {"is_hidden": payload.hidden, "updated_at": datetime.now(timezone.utc)}})
    await record_activity(
        db,
        actor=current_user.get("sub", "staff"),
        actor_role=current_user.get("role", "staff"),
        action="REVIEW_HIDE" if payload.hidden else "REVIEW_SHOW",
        target_type="review",
        target_id=review_id,
        details={"product_id": review.get("product_id"), "rating": review.get("rating")},
    )
    return APIResponse(success=True, message="Đã ẩn đánh giá" if payload.hidden else "Đã hiện lại đánh giá", data={"id": review_id, "is_hidden": payload.hidden})
