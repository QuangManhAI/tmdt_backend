"""Chat trực tuyến khách hàng ↔ cửa hàng.

Khách (đã đăng nhập hoặc vãng lai):   /chat/me ...
Nhân viên / quản trị viên:            /admin/chat ...
Cập nhật tin mới bằng polling: client gửi `after_seq` = seq lớn nhất đã có, server trả các tin sau đó.
"""
import hashlib
import re
import secrets
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Query, status
from motor.motor_asyncio import AsyncIOMotorDatabase
from pymongo import ReturnDocument

from app.core.database import get_db
from app.core.dependencies import get_current_user_token, require_role
from app.core.responses import APIResponse
from app.modules.chat.models import (
    ChatMessageDoc,
    ChatMessageOut,
    ConversationDoc,
    ConversationOut,
    MyChatOut,
    SendMessageRequest,
    StaffSendMessageRequest,
    StaffUnreadOut,
    UpdateConversationRequest,
)

router = APIRouter(tags=["Chat trực tuyến"])

CONVERSATIONS = "conversations"
MESSAGES = "chat_messages"
PREVIEW_LENGTH = 120
STAFF_ROLES = ["admin", "staff"]


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: Optional[datetime]) -> Optional[datetime]:
    # Motor trả datetime không kèm múi giờ (đã là UTC); gắn UTC để JSON có "+00:00", trình duyệt không hiểu nhầm giờ địa phương
    if value is not None and value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


def _clean_body(body: str) -> str:
    # Lưu nguyên văn bản (frontend hiển thị dạng text, React tự escape); chỉ bỏ ký tự điều khiển và dòng trống thừa
    text = re.sub(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]", "", body)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    if not text:
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Tin nhắn không được để trống")
    return text


def _conv_out(doc: dict) -> ConversationOut:
    return ConversationOut(
        id=doc["id"],
        customer_name=doc.get("customer_name", "Khách"),
        customer_phone=doc.get("customer_phone"),
        customer_email=doc.get("customer_email"),
        is_guest=not doc.get("user_id"),
        status=doc.get("status", "open"),
        message_count=doc.get("message_count", 0),
        last_message_preview=doc.get("last_message_preview"),
        last_sender_role=doc.get("last_sender_role"),
        last_message_at=_utc(doc.get("last_message_at")),
        unread_for_staff=doc.get("unread_for_staff", 0),
        unread_for_customer=doc.get("unread_for_customer", 0),
        created_at=_utc(doc.get("created_at")) or _now(),
    )


def _msg_out(doc: dict) -> ChatMessageOut:
    return ChatMessageOut(
        id=doc["id"],
        seq=doc["seq"],
        sender_role=doc["sender_role"],
        sender_name=doc.get("sender_name", ""),
        body=doc["body"],
        created_at=_utc(doc["created_at"]),
    )


async def _messages(db, conversation_id: str, after_seq: int, limit: int) -> List[ChatMessageOut]:
    if after_seq > 0:
        cursor = db[MESSAGES].find({"conversation_id": conversation_id, "seq": {"$gt": after_seq}}).sort("seq", 1).limit(limit)
        return [_msg_out(d) async for d in cursor]
    # Lần tải đầu: lấy `limit` tin mới nhất, trả theo thứ tự cũ → mới
    cursor = db[MESSAGES].find({"conversation_id": conversation_id}).sort("seq", -1).limit(limit)
    docs = [d async for d in cursor]
    return [_msg_out(d) for d in reversed(docs)]


async def _append_message(db, conv: dict, sender_role: str, sender_id: Optional[str], sender_name: str, body: str) -> ChatMessageOut:
    """Cấp seq nguyên tử, cập nhật thông tin tóm tắt + bộ đếm chưa đọc, rồi lưu tin."""
    now = _now()
    unread_field = "unread_for_staff" if sender_role == "customer" else "unread_for_customer"
    updated = await db[CONVERSATIONS].find_one_and_update(
        {"id": conv["id"]},
        {
            "$inc": {"message_count": 1, unread_field: 1},
            "$set": {
                "last_message_preview": body[:PREVIEW_LENGTH],
                "last_sender_role": sender_role,
                "last_message_at": now,
                "updated_at": now,
                # Khách nhắn vào hội thoại đã đóng thì mở lại
                **({"status": "open"} if sender_role == "customer" else {}),
            },
        },
        return_document=ReturnDocument.AFTER,
    )
    msg = ChatMessageDoc(
        conversation_id=conv["id"],
        seq=updated["message_count"],
        sender_role=sender_role,
        sender_id=sender_id,
        sender_name=sender_name,
        body=body,
        created_at=now,
    )
    await db[MESSAGES].insert_one(msg.model_dump())
    return _msg_out(msg.model_dump())


# ---------------------------------------------------------------------------
# Phía khách hàng
# ---------------------------------------------------------------------------

async def _find_my_conversation(db, token_payload: Optional[dict], chat_token: Optional[str]) -> Optional[dict]:
    col = db[CONVERSATIONS]
    user_id = token_payload.get("sub") if token_payload else None
    guest_hash = _hash_token(chat_token) if chat_token else None

    if user_id:
        conv = await col.find_one({"user_id": user_id})
        if conv:
            return conv
        # Khách vãng lai vừa đăng nhập: nhận hội thoại cũ về tài khoản
        if guest_hash:
            conv = await col.find_one({"guest_token_hash": guest_hash, "user_id": {"$exists": False}})
            if conv:
                user = await db["users"].find_one({"id": user_id}) or {}
                updates = {"user_id": user_id, "updated_at": _now()}
                if user.get("full_name"):
                    updates["customer_name"] = user["full_name"]
                if user.get("phone"):
                    updates["customer_phone"] = user["phone"]
                if user.get("email"):
                    updates["customer_email"] = user["email"]
                await col.update_one({"id": conv["id"]}, {"$set": updates, "$unset": {"guest_token_hash": ""}})
                conv.update(updates)
                conv.pop("guest_token_hash", None)
                return conv
        return None

    if guest_hash:
        return await col.find_one({"guest_token_hash": guest_hash})
    return None


@router.get("/chat/me", response_model=APIResponse[MyChatOut])
async def get_my_chat(
    after_seq: int = Query(0, ge=0),
    limit: int = Query(50, ge=0, le=100),
    token_payload: Optional[dict] = Depends(get_current_user_token),
    x_chat_token: Optional[str] = Header(None),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Hội thoại của khách hiện tại (null nếu chưa nhắn lần nào). limit=0: chỉ lấy số tin chưa đọc (polling khi khung chat đóng)."""
    conv = await _find_my_conversation(db, token_payload, x_chat_token)
    if not conv:
        return APIResponse(data=MyChatOut())
    messages = await _messages(db, conv["id"], after_seq, limit) if limit else []
    return APIResponse(data=MyChatOut(conversation=_conv_out(conv), messages=messages))


@router.post("/chat/me/messages", response_model=APIResponse[MyChatOut])
async def send_my_message(
    payload: SendMessageRequest,
    token_payload: Optional[dict] = Depends(get_current_user_token),
    x_chat_token: Optional[str] = Header(None),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Khách gửi tin. Chưa có hội thoại thì tạo mới; khách vãng lai nhận guest_token để dùng cho các lần sau."""
    body = _clean_body(payload.body)
    conv = await _find_my_conversation(db, token_payload, x_chat_token)
    guest_token = None
    user = None
    if token_payload:
        user = await db["users"].find_one({"id": token_payload.get("sub")})
        if not user:
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail="Phiên đăng nhập không hợp lệ")

    if not conv:
        if user:
            doc = ConversationDoc(
                user_id=user["id"],
                customer_name=user.get("full_name") or "Khách hàng",
                customer_phone=user.get("phone"),
                customer_email=user.get("email"),
            )
        else:
            name = (payload.name or "").strip()
            if len(name) < 2:
                raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail="Vui lòng nhập tên để cửa hàng tiện xưng hô")
            guest_token = secrets.token_urlsafe(32)
            doc = ConversationDoc(
                guest_token_hash=_hash_token(guest_token),
                customer_name=name,
                customer_phone=re.sub(r"[^\d+]", "", payload.phone or "") or None,
            )
        conv = doc.model_dump(exclude_none=True)
        await db[CONVERSATIONS].insert_one(conv)

    sender_name = user.get("full_name") if user else conv.get("customer_name", "Khách")
    msg = await _append_message(db, conv, "customer", user["id"] if user else None, sender_name, body)
    fresh = await db[CONVERSATIONS].find_one({"id": conv["id"]})
    return APIResponse(
        message="Đã gửi tin nhắn",
        data=MyChatOut(conversation=_conv_out(fresh), messages=[msg], guest_token=guest_token),
    )


@router.post("/chat/me/read", response_model=APIResponse[dict])
async def mark_my_chat_read(
    token_payload: Optional[dict] = Depends(get_current_user_token),
    x_chat_token: Optional[str] = Header(None),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    conv = await _find_my_conversation(db, token_payload, x_chat_token)
    if conv:
        await db[CONVERSATIONS].update_one({"id": conv["id"]}, {"$set": {"unread_for_customer": 0}})
    return APIResponse(data={"read": True})


# ---------------------------------------------------------------------------
# Phía nhân viên / quản trị viên
# ---------------------------------------------------------------------------

async def _get_conv_or_404(db, conversation_id: str) -> dict:
    conv = await db[CONVERSATIONS].find_one({"id": conversation_id})
    if not conv:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Không tìm thấy hội thoại")
    return conv


@router.get("/admin/chat/conversations", response_model=APIResponse[List[ConversationOut]])
async def list_conversations(
    status_filter: str = Query("all", alias="status", pattern="^(all|open|closed|unread)$"),
    limit: int = Query(50, ge=1, le=200),
    _: dict = Depends(require_role(STAFF_ROLES)),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    query: dict = {"message_count": {"$gt": 0}}
    if status_filter in ("open", "closed"):
        query["status"] = status_filter
    elif status_filter == "unread":
        query["unread_for_staff"] = {"$gt": 0}
    cursor = db[CONVERSATIONS].find(query).sort("last_message_at", -1).limit(limit)
    return APIResponse(data=[_conv_out(d) async for d in cursor])


@router.get("/admin/chat/unread", response_model=APIResponse[StaffUnreadOut])
async def staff_unread(
    _: dict = Depends(require_role(STAFF_ROLES)),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    """Số hội thoại / tin khách gửi chưa đọc (badge trên tab Tin nhắn)."""
    convs, msgs = 0, 0
    async for d in db[CONVERSATIONS].find({"unread_for_staff": {"$gt": 0}}, {"unread_for_staff": 1}):
        convs += 1
        msgs += d.get("unread_for_staff", 0)
    return APIResponse(data=StaffUnreadOut(conversations=convs, messages=msgs))


@router.get("/admin/chat/conversations/{conversation_id}/messages", response_model=APIResponse[List[ChatMessageOut]])
async def staff_get_messages(
    conversation_id: str,
    after_seq: int = Query(0, ge=0),
    limit: int = Query(100, ge=1, le=200),
    _: dict = Depends(require_role(STAFF_ROLES)),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    await _get_conv_or_404(db, conversation_id)
    return APIResponse(data=await _messages(db, conversation_id, after_seq, limit))


@router.post("/admin/chat/conversations/{conversation_id}/messages", response_model=APIResponse[ChatMessageOut])
async def staff_send_message(
    conversation_id: str,
    payload: StaffSendMessageRequest,
    current_user: dict = Depends(require_role(STAFF_ROLES)),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    conv = await _get_conv_or_404(db, conversation_id)
    body = _clean_body(payload.body)
    staff = await db["users"].find_one({"id": current_user.get("sub")}) or {}
    msg = await _append_message(db, conv, "staff", current_user.get("sub"), staff.get("full_name") or "Cửa hàng", body)
    # Nhân viên đã trả lời thì coi như đã đọc hết tin khách
    await db[CONVERSATIONS].update_one({"id": conversation_id}, {"$set": {"unread_for_staff": 0}})
    return APIResponse(message="Đã gửi tin nhắn", data=msg)


@router.post("/admin/chat/conversations/{conversation_id}/read", response_model=APIResponse[dict])
async def staff_mark_read(
    conversation_id: str,
    _: dict = Depends(require_role(STAFF_ROLES)),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    await _get_conv_or_404(db, conversation_id)
    await db[CONVERSATIONS].update_one({"id": conversation_id}, {"$set": {"unread_for_staff": 0}})
    return APIResponse(data={"read": True})


@router.patch("/admin/chat/conversations/{conversation_id}", response_model=APIResponse[ConversationOut])
async def staff_update_conversation(
    conversation_id: str,
    payload: UpdateConversationRequest,
    _: dict = Depends(require_role(STAFF_ROLES)),
    db: AsyncIOMotorDatabase = Depends(get_db),
):
    await _get_conv_or_404(db, conversation_id)
    updated = await db[CONVERSATIONS].find_one_and_update(
        {"id": conversation_id},
        {"$set": {"status": payload.status, "updated_at": _now()}},
        return_document=ReturnDocument.AFTER,
    )
    return APIResponse(data=_conv_out(updated))
