"""Client gọi API Giao Hàng Nhanh (GHN) theo địa chỉ hành chính mới 2 cấp (tỉnh + phường/xã, từ 01/07/2025).

Tài liệu: https://developer.ghn.vn/en/docs
- Danh mục địa chỉ mới: /v3/master-data/province/all, /v3/master-data/ward/all-by-province-id
- Tạo đơn / xem trước phí: /v2/shipping-order/create, /v2/shipping-order/preview (cờ is_new_to_address)
- Chi tiết đơn: /v2/shipping-order/detail
"""

import logging
import re
import time
import unicodedata
from typing import Any, Dict, List, Optional, Tuple

import httpx

logger = logging.getLogger(__name__)

# Trạng thái vận đơn GHN -> nhãn tiếng Việt hiển thị cho khách
GHN_STATUS_LABELS: Dict[str, str] = {
    "ready_to_pick": "Chờ lấy hàng",
    "picking": "Đang lấy hàng",
    "money_collect_picking": "Đang lấy hàng",
    "cancel": "Vận đơn đã hủy",
    "picked": "Đã lấy hàng",
    "storing": "Đang ở kho GHN",
    "transporting": "Đang luân chuyển",
    "sorting": "Đang phân loại",
    "delivering": "Đang giao hàng",
    "money_collect_delivering": "Đang giao hàng",
    "delivered": "Giao thành công",
    "delivery_fail": "Giao không thành công",
    "waiting_to_return": "Chờ giao lại",
    "return": "Chờ hoàn hàng",
    "return_transporting": "Đang hoàn hàng",
    "return_sorting": "Đang hoàn hàng",
    "returning": "Đang hoàn hàng",
    "return_fail": "Hoàn hàng thất bại",
    "returned": "Đã hoàn hàng về shop",
    "exception": "Đơn gặp sự cố",
    "damage": "Hàng bị hư hỏng",
    "lost": "Hàng bị thất lạc",
}

_PROVINCE_PREFIX = re.compile(r"^(thanh pho|tp\.?|tinh)\s+")
_WARD_PREFIX = re.compile(r"^(phuong|xa|dac khu|thi tran|p\.?|x\.?)\s+")


def _fold(text: str) -> str:
    """Bỏ dấu, chữ thường, gọn khoảng trắng: 'TP. Hồ Chí Minh' -> 'tp. ho chi minh'."""
    text = unicodedata.normalize("NFD", text or "").replace("đ", "d").replace("Đ", "D")
    text = "".join(ch for ch in text if unicodedata.category(ch) != "Mn")
    return " ".join(text.lower().replace(",", " ").split())


def province_key(name: str) -> str:
    return _PROVINCE_PREFIX.sub("", _fold(name)).replace(".", "").strip()


def ward_key(name: str) -> str:
    return _WARD_PREFIX.sub("", _fold(name)).replace(".", "").strip()


class GHNError(Exception):
    """GHN từ chối yêu cầu (sai địa chỉ, sai token...) hoặc không kết nối được."""


class GHNClient:
    def __init__(self, token: str, shop_id: str, base_url: str, timeout: float = 8.0):
        self.token = token
        self.shop_id = str(shop_id)
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout
        self._provinces: Optional[Tuple[float, List[Dict[str, Any]]]] = None
        self._wards: Dict[int, Tuple[float, List[Dict[str, Any]]]] = {}

    async def _request(self, method: str, path: str, *, json: Any = None, params: Any = None) -> Any:
        headers = {"Token": self.token, "ShopId": self.shop_id, "Content-Type": "application/json"}
        try:
            async with httpx.AsyncClient(timeout=self.timeout) as client:
                resp = await client.request(method, f"{self.base_url}{path}", json=json, params=params, headers=headers)
        except httpx.HTTPError as e:
            logger.warning("Không gọi được GHN %s: %r", path, e)
            raise GHNError("Không kết nối được Giao Hàng Nhanh") from e
        try:
            body = resp.json()
        except ValueError:
            logger.warning("GHN trả về không phải JSON: %s HTTP %s %s", path, resp.status_code, resp.text[:200])
            raise GHNError(f"GHN trả về HTTP {resp.status_code}")
        if resp.status_code >= 400 or body.get("code") not in (200, None):
            msg = body.get("code_message_value") or body.get("message_display") or body.get("message") or f"HTTP {resp.status_code}"
            logger.warning("GHN từ chối %s: HTTP %s %s", path, resp.status_code, msg)
            raise GHNError(str(msg))
        return body.get("data")

    # ---------- Danh mục địa chỉ mới (cache 12 giờ, ít thay đổi) ----------
    async def provinces(self) -> List[Dict[str, Any]]:
        if self._provinces and time.monotonic() - self._provinces[0] < 43200:
            return self._provinces[1]
        data = await self._request("GET", "/v3/master-data/province/all") or []
        active = [p for p in data if p.get("status", 1) == 1]
        self._provinces = (time.monotonic(), active)
        return active

    async def wards(self, province_id: int) -> List[Dict[str, Any]]:
        cached = self._wards.get(province_id)
        if cached and time.monotonic() - cached[0] < 43200:
            return cached[1]
        result: List[Dict[str, Any]] = []
        offset = 0
        while True:
            page = await self._request(
                "GET", "/v3/master-data/ward/all-by-province-id",
                params={"province_id": province_id, "offset": offset, "limit": 200},
            ) or []
            result.extend(w for w in page if w.get("status", 1) == 1)
            if len(page) < 200:
                break
            offset += 200
        self._wards[province_id] = (time.monotonic(), result)
        return result

    async def find_province(self, name: str) -> Optional[Dict[str, Any]]:
        key = province_key(name)
        if not key:
            return None
        for p in await self.provinces():
            names = [p.get("name", "")] + list(p.get("extension_names") or [])
            if any(province_key(n) == key for n in names):
                return p
        return None

    async def resolve_address(self, province: str, ward: str) -> Tuple[str, str]:
        """Đổi tên tỉnh / phường khách nhập sang đúng tên trong danh mục GHN (không khớp thì giữ nguyên)."""
        p = await self.find_province(province)
        if not p:
            return province, ward
        ward_name = ward
        key = ward_key(ward)
        if key:
            for w in await self.wards(int(p["_id"])):
                names = [w.get("name", "")] + list(w.get("extension_names") or [])
                if any(ward_key(n) == key for n in names):
                    ward_name = w["name"]
                    break
        return p["name"], ward_name

    # ---------- Đơn hàng ----------
    async def preview_order(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request("POST", "/v2/shipping-order/preview", json=body) or {}

    async def create_order(self, body: Dict[str, Any]) -> Dict[str, Any]:
        return await self._request("POST", "/v2/shipping-order/create", json=body) or {}

    async def order_detail(self, order_code: str) -> Dict[str, Any]:
        return await self._request("GET", "/v2/shipping-order/detail", params={"order_code": order_code}) or {}
