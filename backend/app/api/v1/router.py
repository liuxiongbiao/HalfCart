"""
API v1 路由聚合 — 汇总所有 v1 业务模块路由。
业务路由自动注入鉴权依赖, Swagger 显示锁图标。
"""

from fastapi import APIRouter, Depends

from app.api.deps import get_current_user

# ═══ 业务子路由 ═══
from app.api.v1.auth import router as auth_router
from app.api.v1.admin.dashboard import router as admin_dashboard_router
from app.api.v1.admin.disputes import router as admin_disputes_router
from app.api.v1.admin.orders import router as admin_orders_router
from app.api.v1.admin.reconciliation import router as admin_recon_router
from app.api.v1.admin.users import router as admin_users_router
from app.api.v1.admin.withdraws import router as admin_withdraws_router
from app.api.v1.chat import router as chat_router
from app.api.v1.dispute import router as dispute_router
from app.api.v1.orders import router as orders_router
from app.api.v1.radar import router as radar_router
from app.api.v1.user import router as user_router
from app.api.v1.withdraw import router as withdraw_router

# ═══ 测试子路由 (免鉴权) ═══
from app.api.v1.test_auth import router as test_auth_router
from app.api.v1.test_balance import router as test_balance_router
from app.api.v1.test_sms import router as test_sms_router
from app.api.v1.wechat_mp import router as wechat_mp_router
from app.api.v1.wechat_login import router as wechat_login_router
from app.api.v1.dify_api import router as dify_router
from app.api.v1.pay import router as pay_router
from app.api.v1.lbs import router as lbs_router
from app.api.v1.address import router as address_router
from app.api.v1.categories import router as categories_router
from app.api.v1.pay_account import router as pay_account_router

api_router = APIRouter(dependencies=[Depends(get_current_user)])

# ── 地址管理 (鉴权) ──
api_router.include_router(address_router, prefix="/user", tags=["Address"])
# ── 收款账户管理 (鉴权) ──
api_router.include_router(pay_account_router, prefix="/user", tags=["Payment-Account"])

# ── Dify AI 工作流 (鉴权) ──
api_router.include_router(dify_router, prefix="/dify", tags=["Dify-AI"])
# ── 高德LBS增强接口 (鉴权) ──
api_router.include_router(lbs_router, prefix="/lbs", tags=["LBS-Amap"])

# ── 测试接口 (免鉴权: 独立路由, 不加 security) ──
test_api_router = APIRouter()
# 支付宝支付 (单路由: create/query/refund 需端点级鉴权, notify/return 免鉴权)
test_api_router.include_router(pay_router, prefix="/pay/alipay", tags=["Payment-Alipay"])
# 微信公众号-服务器校验 (免鉴权, GET /api/v1/wechat)
test_api_router.include_router(wechat_mp_router, tags=["Wechat MP"])
# 微信公众号-扫码登录 (免鉴权, /api/v1/wechat/qrcode, /callback, /check)
test_api_router.include_router(wechat_login_router, prefix="/wechat", tags=["Wechat Login"])
# Auth 免鉴权 (登录注册)
test_api_router.include_router(auth_router, prefix="/auth", tags=["Auth"])
test_api_router.include_router(categories_router, prefix="/categories", tags=["Categories"])
test_api_router.include_router(test_balance_router, prefix="/test/balance", tags=["Test Balance"])
test_api_router.include_router(test_auth_router, prefix="/test", tags=["Test Auth"])
test_api_router.include_router(test_sms_router, prefix="/test/sms", tags=["Test SMS"])

# ── 业务接口 (自动鉴权) ──
api_router.include_router(orders_router, prefix="/orders", tags=["Orders"])
api_router.include_router(radar_router, prefix="/radar", tags=["LBS Radar"])
api_router.include_router(user_router, prefix="/user", tags=["User Center"])
api_router.include_router(chat_router, prefix="/chat", tags=["Chat"])
api_router.include_router(dispute_router, prefix="/disputes", tags=["Disputes"])
api_router.include_router(withdraw_router, prefix="/withdraw", tags=["Withdraw"])
api_router.include_router(admin_dashboard_router, prefix="/admin/dashboard", tags=["Admin-Dashboard"])
api_router.include_router(admin_orders_router, prefix="/admin/orders", tags=["Admin-Orders"])
api_router.include_router(admin_users_router, prefix="/admin/users", tags=["Admin-Users"])
api_router.include_router(admin_disputes_router, prefix="/admin/disputes", tags=["Admin-Disputes"])
api_router.include_router(admin_recon_router, prefix="/admin/reconciliation", tags=["Admin-Reconciliation"])
api_router.include_router(admin_withdraws_router, prefix="/admin/withdraws", tags=["Admin-Withdraws"])
