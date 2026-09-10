"""
订单服务测试 — 创建/查询/删除/自参拦截
"""
import pytest
from httpx import ASGITransport, AsyncClient


async def _get_token(client):
    resp = await client.post("/api/v1/test/token", json={"user_id": 1})
    return resp.json()["data"]["access_token"]


@pytest.mark.asyncio
async def test_create_normal_order_succeeds(app):
    """创建普通拼单 → 成功返回 order_id。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _get_token(client)
        resp = await client.post(
            "/api/v1/orders",
            json={
                "goods_name": "维达纸巾",
                "price_per_person": "9.90",
                "total_people": 5,
                "meet_location": "上海人民广场地铁站",
                "location_lat": "31.2304",
                "location_lng": "121.4737",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
    data = resp.json()
    assert data["code"] == 0
    assert data["data"]["order_id"] > 0


@pytest.mark.asyncio
async def test_get_order_detail(app):
    """获取订单详情 → 完整字段。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _get_token(client)
        # 先创建
        create = await client.post(
            "/api/v1/orders",
            json={
                "goods_name": "详情测试",
                "price_per_person": "15.00",
                "total_people": 3,
                "meet_location": "测试",
                "location_lat": "31.2304",
                "location_lng": "121.4737",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        oid = create.json()["data"]["order_id"]

        # 查详情
        resp = await client.get(
            f"/api/v1/orders/{oid}",
            headers={"Authorization": f"Bearer {token}"},
        )
    data = resp.json()
    assert data["code"] == 0
    assert data["data"]["goods_name"] == "详情测试"


@pytest.mark.asyncio
async def test_delete_order_gathering_succeeds(app):
    """GATHERING 状态 → 团长删除成功。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _get_token(client)
        create = await client.post(
            "/api/v1/orders",
            json={
                "goods_name": "待删除",
                "price_per_person": "5.00",
                "total_people": 3,
                "meet_location": "测试",
                "location_lat": "31.2304",
                "location_lng": "121.4737",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        oid = create.json()["data"]["order_id"]
        resp = await client.delete(
            f"/api/v1/orders/{oid}",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.json()["code"] == 0


@pytest.mark.asyncio
async def test_self_join_blocked(app):
    """团长不能自参 → ORDER_SELF_JOIN_DENIED。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _get_token(client)
        create = await client.post(
            "/api/v1/orders",
            json={
                "goods_name": "自参拦截",
                "price_per_person": "10.00",
                "total_people": 3,
                "meet_location": "测试",
                "location_lat": "31.2304",
                "location_lng": "121.4737",
            },
            headers={"Authorization": f"Bearer {token}"},
        )
        oid = create.json()["data"]["order_id"]

        resp = await client.post(
            f"/api/v1/orders/{oid}/join",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.json()["code"] != 0


@pytest.mark.asyncio
async def test_order_not_found(app):
    """不存在的订单 → 错误。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _get_token(client)
        resp = await client.get(
            "/api/v1/orders/9999999",
            headers={"Authorization": f"Bearer {token}"},
        )
    assert resp.json()["code"] != 0
