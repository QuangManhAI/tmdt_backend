"""Schema chat trực tuyến giữa khách hàng và cửa hàng.

Collection `conversations`: mỗi khách có tối đa 1 cuộc hội thoại (theo user_id, hoặc theo token khách vãng lai).
Collection `chat_messages`: từng tin nhắn, tách riêng để hội thoại dài không làm document phình to
(MongoDB giới hạn 16MB/document); thứ tự theo `seq` tăng dần trong mỗi hội thoại.
"""
import uuid
from datetime import datetime, timezone
from typing import List, Literal, Optional

from pydantic import BaseModel, Field

ConversationStatus = Literal["open", "closed"]
SenderRole = Literal["customer", "staff"]

MAX_MESSAGE_LENGTH = 2000


def _now() -> datetime:
    return datetime.now(timezone.utc)


class ConversationDoc(BaseModel):
    """Document trong collection `conversations`.

    Index: id (unique); user_id (unique, sparse); guest_token_hash (unique, sparse);
    (status, last_message_at desc) cho danh sách hộp thư admin.
    user_id / guest_token_hash để trống thì KHÔNG ghi vào document (sparse index bỏ qua trường thiếu, không bỏ qua null).
    """
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    # Chủ hội thoại: khách đã đăng nhập (user_id) hoặc khách vãng lai (sha256 của token bí mật lưu ở trình duyệt)
    user_id: Optional[str] = None
    guest_token_hash: Optional[str] = None
    customer_name: str
    customer_phone: Optional[str] = None
    customer_email: Optional[str] = None
    status: ConversationStatus = "open"
    # Số tin đã gửi = seq của tin mới nhất; tăng nguyên tử khi thêm tin
    message_count: int = 0
    last_message_preview: Optional[str] = None
    last_sender_role: Optional[SenderRole] = None
    last_message_at: Optional[datetime] = None
    unread_for_staff: int = 0
    unread_for_customer: int = 0
    created_at: datetime = Field(default_factory=_now)
    updated_at: datetime = Field(default_factory=_now)


class ChatMessageDoc(BaseModel):
    """Document trong collection `chat_messages`. Index: (conversation_id, seq) unique."""
    id: str = Field(default_factory=lambda: str(uuid.uuid4()))
    conversation_id: str
    seq: int
    sender_role: SenderRole
    sender_id: Optional[str] = None  # user id (khách đã đăng nhập / nhân viên); None với khách vãng lai
    sender_name: str
    body: str
    created_at: datetime = Field(default_factory=_now)


# ---------------------------------------------------------------------------
# Request / Response
# ---------------------------------------------------------------------------

class SendMessageRequest(BaseModel):
    body: str = Field(..., min_length=1, max_length=MAX_MESSAGE_LENGTH)
    # Chỉ dùng khi khách vãng lai gửi tin đầu tiên
    name: Optional[str] = Field(default=None, max_length=100)
    phone: Optional[str] = Field(default=None, max_length=20)


class StaffSendMessageRequest(BaseModel):
    body: str = Field(..., min_length=1, max_length=MAX_MESSAGE_LENGTH)


class UpdateConversationRequest(BaseModel):
    status: ConversationStatus


class ChatMessageOut(BaseModel):
    id: str
    seq: int
    sender_role: SenderRole
    sender_name: str
    body: str
    created_at: datetime


class ConversationOut(BaseModel):
    id: str
    customer_name: str
    customer_phone: Optional[str] = None
    customer_email: Optional[str] = None
    is_guest: bool
    status: ConversationStatus
    message_count: int
    last_message_preview: Optional[str] = None
    last_sender_role: Optional[SenderRole] = None
    last_message_at: Optional[datetime] = None
    unread_for_staff: int
    unread_for_customer: int
    created_at: datetime


class MyChatOut(BaseModel):
    conversation: Optional[ConversationOut] = None
    messages: List[ChatMessageOut] = []
    # Chỉ trả về một lần khi khách vãng lai tạo hội thoại; frontend lưu lại để gửi kèm header X-Chat-Token
    guest_token: Optional[str] = None


class StaffUnreadOut(BaseModel):
    conversations: int
    messages: int
