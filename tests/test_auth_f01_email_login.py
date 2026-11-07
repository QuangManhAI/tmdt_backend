import pytest


@pytest.mark.asyncio
async def test_f01_login_with_dotted_email_after_register(client):
    """F01: Đăng ký → đăng xuất → đăng nhập lại bằng email có dấu chấm (không phân biệt hoa thường)."""
    body = {
        "phone": "0345552200",
        "full_name": "Nguyễn Văn Kiểm Thử",
        "password": "Matkhau123",
        "email": "ten.ho.test@gmail.com",
    }
    r = await client.post("/api/v1/auth/register", json=body)
    assert r.status_code == 200, r.text

    for identifier in ["ten.ho.test@gmail.com", "  Ten.Ho.Test@Gmail.com ", "0345552200", "0345 552 200"]:
        r = await client.post("/api/v1/auth/login", json={"phone": identifier, "password": "Matkhau123"})
        assert r.status_code == 200, (identifier, r.text)
        assert r.json()["data"]["phone"] == "0345552200"

    # Email gần giống (không dấu chấm) không được khớp nhầm
    r = await client.post("/api/v1/auth/login", json={"phone": "tenhotest@gmail.com", "password": "Matkhau123"})
    assert r.status_code == 401
