"""Cập nhật trạng thái vận đơn vào đơn hàng (từ webhook GHN hoặc đồng bộ khi xem đơn)."""

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional

from motor.motor_asyncio import AsyncIOMotorDatabase

from app.modules.shipping.provider import shipping_service

SYNC_INTERVAL = timedelta(minutes=10)

# Trạng thái GHN -> trạng thái đơn của shop (chỉ chuyển theo chiều tiến)
_ORDER_STATUS_FROM_GHN = {"delivered": "delivered"}


def _aware(dt: Optional[datetime]) -> Optional[datetime]:
    if dt is None:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


async def apply_shipment_status(db: AsyncIOMotorDatabase, order: Dict[str, Any], info: Dict[str, Any], source: str) -> Dict[str, Any]:
    """Ghi trạng thái vận đơn mới; GHN báo giao thành công thì đơn chuyển sang 'Đã giao'. Trả về đơn sau cập nhật."""
    now = datetime.now(timezone.utc)
    shipment = dict(order.get("shipment") or {})
    changed = shipment.get("status") != info["status"]
    shipment.update({k: v for k, v in info.items() if v is not None})
    shipment["synced_at"] = now

    update: Dict[str, Any] = {"$set": {"shipment": shipment, "updated_at": now}}
    if changed:
        update["$push"] = {"shipment_events": {"status": info["status"], "status_label": info.get("status_label"), "time": now, "source": source}}

    target = _ORDER_STATUS_FROM_GHN.get(info["status"])
    if changed and target and order.get("status") == "shipping":
        update["$set"]["status"] = target
        if target == "delivered":
            update["$set"]["delivered_at"] = now
        update.setdefault("$push", {})
        # $push nhiều field trong cùng một lệnh
        update["$push"]["status_history"] = {
            "old_status": order.get("status"),
            "new_status": target,
            "changed_by": order.get("carrier") or "carrier",
            "timestamp": now,
            "note": f"{order.get('carrier', 'Đơn vị vận chuyển')}: {info.get('status_label') or info['status']}",
        }

    await db["orders"].update_one({"order_code": order["order_code"]}, update)
    return await db["orders"].find_one({"order_code": order["order_code"]})


async def maybe_sync_shipment(db: AsyncIOMotorDatabase, order: Dict[str, Any]) -> Dict[str, Any]:
    """Đơn đang giao mà lâu chưa cập nhật (webhook lỡ nhịp) thì hỏi lại GHN. Lỗi thì giữ nguyên đơn."""
    if order.get("status") != "shipping" or order.get("carrier") != shipping_service.name or not order.get("tracking_code"):
        return order
    synced = _aware((order.get("shipment") or {}).get("synced_at"))
    if synced and datetime.now(timezone.utc) - synced < SYNC_INTERVAL:
        return order
    info = await shipping_service.get_status(order["tracking_code"])
    if not info:
        return order
    return await apply_shipment_status(db, order, info, source="sync")
