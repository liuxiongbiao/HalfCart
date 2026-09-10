"""
用户服务测试 — 个人信息/更新/首页
"""
import pytest
from httpx import ASGITransport, AsyncClient


async def _get_token(client):
    resp = await client.post("/api/v1/test/token", json={"user_id": 1})
    return resp.json()["data"]["access_token"]


@pytest.mark.asyncio
async def test_get_profile_with_valid_token(app):
    """有效 Token → 获取个人信息成功。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _get_token(client)
        resp = await client.get(
            "/api/v1/user/profile",
            headers={"Authorization": f"Bearer {token}"},
        )
    data = resp.json()
    assert data["code"] == 0
    assert "nickname" in data["data"]


@pytest.mark.asyncio
async def test_home_page_returns_aggregation(app):
    """首页聚合 → 返回统计。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _get_token(client)
        resp = await client.get(
            "/api/v1/user/home",
            headers={"Authorization": f"Bearer {token}"},
        )
    data = resp.json()
    assert data["code"] == 0


@pytest.mark.asyncio
async def test_update_profile_succeeds(app):
    """更新昵称 → 成功。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _get_token(client)
        resp = await client.put(
            "/api/v1/user/profile",
            json={"nickname": "测试昵称_new", "avatar_url": ""},
            headers={"Authorization": f"Bearer {token}"},
        )
    data = resp.json()
    assert data["code"] == 0
