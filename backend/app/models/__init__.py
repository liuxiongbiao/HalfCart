"""
ORM 模型注册 — 所有模型必须在此导入以注册到 SQLAlchemy metadata。
在 FastAPI 启动时自动加载，Alembic 迁移也依赖此导入。
"""

from app.models.base import BaseModel  # noqa: F401
from app.models.order import Order  # noqa: F401
from app.models.order_participant import OrderParticipant  # noqa: F401
from app.models.payment_log import PaymentLog  # noqa: F401
from app.models.user import User  # noqa: F401
from app.models.verification_code import VerificationCode  # noqa: F401
from app.models.chat_message import ChatMessage  # noqa: F401
from app.models.sms_log import SmsLog  # noqa: F401
from app.models.reconciliation import ReconciliationRecord, ReconciliationAnomaly  # noqa: F401
from app.models.dispute import Dispute  # noqa: F401
from app.models.withdraw import WithdrawApplication  # noqa: F401
from app.models.user_address import UserAddress  # noqa: F401
from app.models.alipay_record import AlipayRecord  # noqa: F401
from app.models.category import Category  # noqa: F401
