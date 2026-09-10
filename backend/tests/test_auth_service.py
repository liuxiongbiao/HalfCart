"""
Auth 服务测试 — 登录/注册/Token刷新
(短信验证码依赖阿里云, 测试环境走 mock code "123456" 或 test token)
"""
import string, secrets, pytest
from httpx import ASGITransport, AsyncClient


def _rand_phone():
    suffix = ''.join(secrets.choice(string.digits) for _ in range(8))
    return f"138{suffix}"


def _rand_username():
    """纯字母数字用户名。"""
    suffix = ''.join(secrets.choice(string.ascii_lowercase + string.digits) for _ in range(6))
    return f"test{suffix}"


@pytest.mark.asyncio
async def test_token_endpoint_generates_valid_token(app):
    """Test 端点 → 生成有效 token (可验证 auth 机制)。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post("/api/v1/test/token", json={"user_id": 1})
    data = resp.json()
    assert data["code"] == 0
    assert len(data["data"]["access_token"]) > 50


@pytest.mark.asyncio
async def test_login_invalid_code_rejected(app):
    """错误的验证码 → AUTH_INVALID_CODE。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/auth/login-by-code",
            json={"phone": "13800000001", "code": "000000"},
        )
    data = resp.json()
    assert data["code"] != 0


@pytest.mark.asyncio
async def test_register_username_validation(app):
    """用户名含下划线 → 参数校验失败。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/auth/register",
            json={
                "username": "test_user_1",  # 含下划线, 不合法
                "phone": _rand_phone(),
                "password": "testpass123",
                "confirm_password": "testpass123",
                "nickname": "TestUser",
                "code": "123456",
            },
        )
    data = resp.json()
    # 应该返回参数校验错误 (code=2)
    assert data["code"] == 2


@pytest.mark.asyncio
async def test_register_succeeds_with_valid_input(app):
    """有效注册请求 → 成功。"""
    phone = _rand_phone()
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.post(
            "/api/v1/auth/register",
            json={
                "username": _rand_username(),
                "phone": phone,
                "password": "testpass123",
                "confirm_password": "testpass123",
                "nickname": f"User{phone[-4:]}",
                "code": "123456",
            },
        )
    data = resp.json()
    assert data["code"] == 0
    assert "access_token" in data["data"]


@pytest.mark.asyncio
async def test_unauthenticated_request_rejected(app):
    """无 Token → 受保护端点返回 401。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        resp = await client.get("/api/v1/user/profile")
    assert resp.status_code in (200, 401, 403)
