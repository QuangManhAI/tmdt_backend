"""Đơn vị vận chuyển: giả lập (mặc định) hoặc Giao Hàng Nhanh khi có GHN_TOKEN + GHN_SHOP_ID.

Luồng:
- Checkout / đặt hàng: quote() báo phí. Đơn từ FREESHIP_THRESHOLD trở lên khách được miễn phí (shop chịu phí GHN).
- Admin chuyển đơn sang "Đang giao": create_shipment() tạo vận đơn, lưu tracking_code + shipment vào đơn.
- GHN báo trạng thái qua webhook, hoặc get_status() đồng bộ lại khi khách xem đơn.
"""

import logging
import time
import uuid
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional

from app.core.config import settings
from app.modules.shipping.ghn import GHN_STATUS_LABELS, GHNClient, GHNError, ward_key

logger = logging.getLogger(__name__)

FREESHIP_THRESHOLD = 500000
ITEM_WEIGHT_GRAMS = 500  # một bộ võ phục đã đóng gói khoảng 0,4 - 0,6 kg


class ShippingError(Exception):
    """Không tạo được vận đơn; message hiển thị cho admin."""


def _items_quantity(items: List[Dict[str, Any]]) -> int:
    return max(sum(int(it.get("quantity", 1)) for it in items), 1)


class ShippingProvider(ABC):
    name: str = ""
    freeship_threshold: int = FREESHIP_THRESHOLD

    def customer_fee(self, carrier_fee: int, subtotal: int) -> int:
        """Phí khách trả: miễn phí khi đạt ngưỡng, ngược lại bằng phí đơn vị vận chuyển."""
        if subtotal > 0 and subtotal >= self.freeship_threshold:
            return 0
        return int(carrier_fee)

    @abstractmethod
    async def quote(self, address: Dict[str, Any], quantity: int = 1, subtotal: int = 0) -> int:
        """Phí khách trả cho địa chỉ và số lượng sản phẩm."""

    @abstractmethod
    async def create_shipment(self, order: Dict[str, Any]) -> Dict[str, Any]:
        """Tạo vận đơn. Trả về {tracking_code, carrier, shipment: {...}}."""

    @abstractmethod
    async def get_status(self, tracking_code: str) -> Optional[Dict[str, Any]]:
        """Trạng thái hiện tại của vận đơn: {status, status_label, expected_delivery_time} hoặc None."""

    def tracking_url(self, tracking_code: str) -> Optional[str]:
        return None


class MockShippingProvider(ShippingProvider):
    """Giả lập theo đặc tả Mục 7: nội thành TP.HCM 25.000đ, tỉnh khác 40.000đ, miễn phí từ 500.000đ."""

    name = "MockShipping"

    def __init__(self, freeship_threshold: int = FREESHIP_THRESHOLD):
        self.freeship_threshold = freeship_threshold
        self.hcm_fee = 25000
        self.other_province_fee = 40000

    def carrier_fee(self, address: Dict[str, Any]) -> int:
        city = str(address.get("city_province", "")).lower()
        if any(hcm in city for hcm in ["hồ chí minh", "tp.hcm", "hcm", "tp hcm", "saigon", "sài gòn"]):
            return self.hcm_fee
        return self.other_province_fee

    async def quote(self, address: Dict[str, Any], quantity: int = 1, subtotal: int = 0) -> int:
        return self.customer_fee(self.carrier_fee(address), subtotal)

    def mock_tracking_code(self) -> str:
        return f"DT-SHIP-{uuid.uuid4().hex[:8].upper()}"

    async def create_shipment(self, order: Dict[str, Any]) -> Dict[str, Any]:
        code = order.get("tracking_code") or self.mock_tracking_code()
        return {
            "tracking_code": code,
            "carrier": self.name,
            "shipment": {
                "status": "ready_to_pick",
                "status_label": GHN_STATUS_LABELS["ready_to_pick"],
                "fee": self.carrier_fee(order.get("shipping_address") or {}),
                "expected_delivery_time": None,
            },
        }

    async def get_status(self, tracking_code: str) -> Optional[Dict[str, Any]]:
        return None


class GHNShippingProvider(ShippingProvider):
    name = "GHN"

    def __init__(self, client: GHNClient, fallback: MockShippingProvider):
        self.client = client
        self.fallback = fallback
        self.freeship_threshold = fallback.freeship_threshold
        self._quote_cache: Dict[tuple, tuple] = {}

    # ---------- Dữ liệu gửi GHN ----------
    def _sender(self) -> Dict[str, Any]:
        """Địa chỉ lấy hàng / hoàn hàng. Bỏ trống GHN_FROM_ADDRESS thì GHN dùng địa chỉ trong hồ sơ cửa hàng."""
        if not settings.GHN_FROM_ADDRESS.strip():
            return {}
        sender = {
            "from_name": settings.GHN_FROM_NAME,
            "from_address": settings.GHN_FROM_ADDRESS,
            "from_ward_name": settings.GHN_FROM_WARD,
            "from_province_name": settings.GHN_FROM_PROVINCE,
            "is_new_from_address": True,
            "return_address": settings.GHN_FROM_ADDRESS,
            "return_ward_name": settings.GHN_FROM_WARD,
            "return_province_name": settings.GHN_FROM_PROVINCE,
            "is_new_return_address": True,
        }
        if settings.GHN_FROM_PHONE.strip():
            sender["from_phone"] = settings.GHN_FROM_PHONE
            sender["return_phone"] = settings.GHN_FROM_PHONE
        return sender

    async def _recipient(self, address: Dict[str, Any]) -> Dict[str, Any]:
        province, ward = await self.client.resolve_address(
            str(address.get("city_province", "")), str(address.get("ward", ""))
        )
        street = str(address.get("street_address", "")).strip()
        return {
            "to_name": address.get("recipient_name") or "Khách hàng",
            "to_phone": address.get("phone") or "0900000000",
            "to_address": ", ".join(p for p in [street, ward, province] if p) or province,
            "to_ward_name": ward,
            "to_province_name": province,
            "is_new_to_address": True,
        }

    @staticmethod
    def _package(quantity: int) -> Dict[str, Any]:
        return {
            "weight": quantity * ITEM_WEIGHT_GRAMS,
            "length": 35,
            "width": 25,
            "height": min(5 * quantity, 60),
            "service_type_id": 2,
        }

    # ---------- Báo phí ----------
    async def quote(self, address: Dict[str, Any], quantity: int = 1, subtotal: int = 0) -> int:
        if subtotal > 0 and subtotal >= self.freeship_threshold:
            return 0  # shop chịu phí, khỏi gọi GHN
        if not str(address.get("ward", "")).strip():
            # Chưa có phường thì GHN không tính được, tạm báo phí giả lập theo tỉnh
            return await self.fallback.quote(address, quantity, subtotal)

        key = (address.get("city_province"), ward_key(str(address.get("ward") or "")), quantity)
        cached = self._quote_cache.get(key)
        if cached and time.monotonic() - cached[0] < 600:
            return self.customer_fee(cached[1], subtotal)
        try:
            body = {
                **self._sender(),
                **(await self._recipient(address)),
                **self._package(quantity),
                "payment_type_id": 1,
                "required_note": settings.GHN_REQUIRED_NOTE,
                "content": "Võ phục",
                "items": [{"name": "Võ phục", "quantity": quantity, "weight": ITEM_WEIGHT_GRAMS}],
            }
            data = await self.client.preview_order(body)
            fee = int(data.get("total_fee") or (data.get("fee") or {}).get("main_service") or 0)
            if fee <= 0:
                raise GHNError("GHN không trả về phí")
        except GHNError as e:
            logger.info("Báo phí GHN lỗi, dùng phí giả lập: %s", e)
            return await self.fallback.quote(address, quantity, subtotal)
        self._quote_cache[key] = (time.monotonic(), fee)
        return self.customer_fee(fee, subtotal)

    # ---------- Vận đơn ----------
    async def create_shipment(self, order: Dict[str, Any]) -> Dict[str, Any]:
        items = order.get("items") or []
        quantity = _items_quantity(items)
        unpaid_cod = str(order.get("payment_method", "")).upper() == "COD" and order.get("payment_status") != "paid"
        goods_value = int(order.get("subtotal", 0)) + int(order.get("embroidery_total", 0))
        try:
            body = {
                **self._sender(),
                **(await self._recipient(order.get("shipping_address") or {})),
                **self._package(quantity),
                "client_order_code": order["order_code"],
                "payment_type_id": 1,  # shop trả phí GHN (khách đã trả phí ship trong tổng đơn)
                "cod_amount": int(order.get("total_amount", 0)) if unpaid_cod else 0,
                "insurance_value": min(goods_value, 5_000_000),
                "required_note": settings.GHN_REQUIRED_NOTE,
                "content": f"Võ phục - đơn {order['order_code']}"[:2000],
                "items": [
                    {
                        "name": f"{it.get('product_name', 'Võ phục')} {it.get('size', '')}".strip()[:100],
                        "code": it.get("sku", ""),
                        "quantity": int(it.get("quantity", 1)),
                        "price": int(it.get("price", 0)),
                        "weight": ITEM_WEIGHT_GRAMS,
                    }
                    for it in items
                ],
            }
            data = await self.client.create_order(body)
        except GHNError as e:
            raise ShippingError(f"GHN không tạo được vận đơn: {e}") from e

        code = data.get("order_code")
        if not code:
            raise ShippingError("GHN không trả về mã vận đơn")
        return {
            "tracking_code": code,
            "carrier": self.name,
            "shipment": {
                "status": "ready_to_pick",
                "status_label": GHN_STATUS_LABELS["ready_to_pick"],
                "fee": int(data.get("total_fee") or 0),
                "expected_delivery_time": data.get("expected_delivery_time"),
                "cod_amount": body["cod_amount"],
            },
        }

    async def get_status(self, tracking_code: str) -> Optional[Dict[str, Any]]:
        try:
            data = await self.client.order_detail(tracking_code)
        except GHNError as e:
            logger.info("Không lấy được trạng thái GHN %s: %s", tracking_code, e)
            return None
        status = data.get("status")
        if not status:
            return None
        return {
            "status": status,
            "status_label": GHN_STATUS_LABELS.get(status, status),
            "expected_delivery_time": data.get("leadtime"),
        }

    def tracking_url(self, tracking_code: str) -> Optional[str]:
        base = settings.GHN_TRACKING_URL.strip()
        return f"{base}{tracking_code}" if base else None


def build_shipping_service() -> ShippingProvider:
    mock = MockShippingProvider()
    if settings.GHN_TOKEN.strip() and str(settings.GHN_SHOP_ID).strip():
        client = GHNClient(settings.GHN_TOKEN.strip(), str(settings.GHN_SHOP_ID).strip(), settings.GHN_BASE_URL)
        return GHNShippingProvider(client, mock)
    return mock


shipping_service: ShippingProvider = build_shipping_service()
