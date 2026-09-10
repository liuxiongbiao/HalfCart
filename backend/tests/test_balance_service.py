"""
资金服务测试 — 钱包增加/冻结+解冻
"""
import string, secrets, pytest
from httpx import ASGITransport, AsyncClient


def _id_key():
    return "test_" + ''.join(secrets.choice(string.hexdigits.lower()) for _ in range(24))


async def _get_token(client):
    resp = await client.post("/api/v1/test/token", json={"user_id": 1})
    return resp.json()["data"]["access_token"]


async def _make_order(client, token):
    """创建测试订单, 返回 order_id。"""
    resp = await client.post(
        "/api/v1/orders",
        json={
            "goods_name": "资金测试商品",
            "price_per_person": "50.00",
            "total_people": 2,
            "meet_location": "测试",
            "location_lat": "31.2304",
            "location_lng": "121.4737",
        },
        headers={"Authorization": f"Bearer {token}"},
    )
    return resp.json()["data"]["order_id"]


@pytest.mark.asyncio
async def test_wallet_increase_works(app):
    """钱包余额增加 → 金额正确。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _get_token(client)
        oid = await _make_order(client, token)
        resp = await client.post(
            "/api/v1/test/balance/wallet-increase",
            json={
                "user_id": 1,
                "order_id": oid,
                "amount": "100.00",
                "idempotent_key": _id_key(),
            },
        )
    data = resp.json()
    assert data["code"] == 0
    assert float(data["data"]["wallet_balance"]) == 100.0


@pytest.mark.asyncio
async def test_freeze_and_unfreeze_flow(app):
    """冻结增加 → 解冻扣减 → 归零。"""
    transport = ASGITransport(app=app)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        token = await _get_token(client)
        oid = await _make_order(client, token)

        # 冻结
        freeze = await client.post(
            "/api/v1/test/balance/freeze-increase",
            json={
                "user_id": 1,
                "order_id": oid,
                "amount": "80.00",
                "idempotent_key": _id_key(),
            },
        )
        assert freeze.json()["code"] == 0

        # 解冻
        unfreeze = await client.post(
            "/api/v1/test/balance/freeze-decrease",
            json={
                "user_id": 1,
                "order_id": oid,
                "amount": "80.00",
                "idempotent_key": _id_key(),
            },
        )
        assert unfreeze.json()["code"] == 0
