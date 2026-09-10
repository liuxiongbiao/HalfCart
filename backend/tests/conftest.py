"""
HalfCart 测试共享 Fixtures
==========================
Mock Redis/ES/MQ, 只测试 MySQL + 业务逻辑。
"""
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

# ── 环境变量 ──
os.environ.setdefault("APP_ENV", "development")
os.environ.setdefault("TEST_MODE", "true")
os.environ.setdefault("MOCK_PAYMENT", "true")
os.environ.setdefault("DB_ECHO", "false")


# ── 构建 mock 对象 ──
class _FakeRedis:
    def __init__(self):
        self._store = {}

    async def get(self, key):
        return self._store.get(key)

    async def set(self, key, value, *args, **kwargs):
        self._store[key] = value
        return True

    async def delete(self, *keys):
        for k in keys:
            self._store.pop(k, None)
        return len(keys)

    async def exists(self, key):
        return key in self._store

    async def expire(self, *args, **kwargs):
        return True

    async def incr(self, key):
        self._store[key] = self._store.get(key, 0) + 1
        return self._store[key]

    async def ttl(self, key):
        return -1

    def __getattr__(self, name):
        return lambda *a, **kw: True


class _FakeES:
    async def index(self, *a, **kw):
        return {"result": "created"}

    async def search(self, *a, **kw):
        return {"hits": {"hits": []}}

    async def delete(self, *a, **kw):
        return {"result": "deleted"}

    async def update(self, *a, **kw):
        return {"result": "updated"}

    async def exists(self, *a, **kw):
        return False

    def __getattr__(self, name):
        return lambda *a, **kw: MagicMock()


# ── Monkey patch — 必须在 app 导入之前执行 ──
_patches = {
    "redis_init": patch("app.core.redis_client.init_redis", new_callable=AsyncMock),
    "redis_close": patch("app.core.redis_client.close_redis", new_callable=AsyncMock),
    "redis_get": patch("app.core.redis_client.get_redis", return_value=_FakeRedis()),
    "es_init": patch("app.core.es_client.init_es", new_callable=AsyncMock),
    "es_close": patch("app.core.es_client.close_es", new_callable=AsyncMock),
    "es_get": patch("app.core.es_client.get_es", return_value=_FakeES()),
    "mq_init": patch("app.core.mq_client.init_mq", new_callable=AsyncMock),
    "mq_close": patch("app.core.mq_client.close_mq", new_callable=AsyncMock),
    "mq_publish": patch("app.core.mq_client.publish_message", new_callable=AsyncMock),
    "mq_order_notify": patch("app.core.mq_client.publish_order_notify", new_callable=AsyncMock),
    "mq_es_sync": patch("app.core.mq_client.publish_order_es_sync", new_callable=AsyncMock),
    "mq_ai_asr": patch("app.core.mq_client.publish_ai_asr_task", new_callable=AsyncMock),
}

for _p in _patches.values():
    _p.start()

# ── Mock SMS / 验证码: code="123456" 通过, 其他拒绝 ──
async def _mock_verify_code(phone: str, code: str) -> bool:
    return code == "123456"

async def _mock_aliyun_check(phone: str, code: str, scene: str = "login") -> bool:
    return code == "123456"

async def _mock_verify_sms(phone: str, code: str, scene: str = "login") -> bool:
    return code == "123456"

_mock_send = AsyncMock(return_value=None)
patch("app.services.auth_service._verify_code_hybrid", side_effect=_mock_verify_code).start()
patch("app.services.auth_service.send_login_code", new=_mock_send).start()
patch("app.services.sms_service.check_code_via_aliyun", side_effect=_mock_aliyun_check).start()
patch("app.services.sms_service.verify_code", side_effect=_mock_verify_sms).start()


# ── 现在安全导入 app ──
import app.main as _app_module  # noqa: E402


# ── Pytest fixtures ──


@pytest.fixture(scope="function")
def app():
    """FastAPI app 实例 (function 级别, 每测试独立, 避免 event loop 冲突)。"""
    return _app_module.app
