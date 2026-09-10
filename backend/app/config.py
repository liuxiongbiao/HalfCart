"""
半仓智拼 (HalfCart) 全局配置中心
===============================
基于 pydantic-settings，所有配置从环境变量读取。
对应PRD：全链路配置管理，包含MySQL、Redis、ES、RabbitMQ、JWT、Dify、Mock支付等全部配置项。

使用方式:
    from app.config import settings
    db_url = settings.DATABASE_URL
"""

from functools import lru_cache
from typing import Literal

from pydantic import Field, SecretStr
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """应用全局配置，读取 .env 文件与环境变量。"""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # ──────────────────────────────────────────────
    # 应用基础
    # ──────────────────────────────────────────────
    APP_NAME: str = Field(default="HalfCart", description="应用名称")
    APP_VERSION: str = Field(default="1.0.0", description="应用版本")
    APP_ENV: Literal["development", "staging", "production"] = Field(
        default="development", description="运行环境"
    )
    DEBUG: bool = Field(default=False, description="调试模式")
    LOG_LEVEL: str = Field(default="INFO", description="日志级别")

    # CORS 白名单 (逗号分隔)
    CORS_ALLOWED_ORIGINS: str = Field(
        default="http://localhost:3000,http://localhost,http://127.0.0.1:3000,http://127.0.0.1",
        description="CORS 允许的前端域名 (逗号分隔)"
    )

    # ──────────────────────────────────────────────
    # 数据库 MySQL (PRD 1.2: 核心交易数据持久化)
    # ──────────────────────────────────────────────
    DB_HOST: str = Field(default="localhost", description="数据库主机名")
    DB_PORT: int = Field(default=3306, description="数据库端口")
    DB_USER: str = Field(default="halfcart", description="数据库用户名")
    DB_PASSWORD: SecretStr = Field(
        default=SecretStr("halfcart123"), description="数据库密码"
    )
    DB_NAME: str = Field(default="halfcart", description="数据库名")
    DB_POOL_SIZE: int = Field(default=20, description="连接池大小")
    DB_MAX_OVERFLOW: int = Field(default=40, description="连接池溢出上限")
    DB_POOL_RECYCLE: int = Field(default=3600, description="连接回收时间(秒)")
    DB_ECHO: bool = Field(default=False, description="SQL日志输出")

    @property
    def DATABASE_URL(self) -> str:
        """构造异步MySQL连接URL（aiomysql驱动）。
        PRD 5.3: 资金字段明文存储，通过访问白名单+应用层验签+流水对账保障安全。
        """
        password = self.DB_PASSWORD.get_secret_value()
        return (
            f"mysql+aiomysql://{self.DB_USER}:{password}"
            f"@{self.DB_HOST}:{self.DB_PORT}/{self.DB_NAME}"
            f"?charset=utf8mb4"
        )

    # ──────────────────────────────────────────────
    # Redis (PRD 4.3/4.5/5.1: 分布式锁/LBS缓存/验证码/限流)
    # ──────────────────────────────────────────────
    REDIS_HOST: str = Field(default="localhost", description="Redis主机名")
    REDIS_PORT: int = Field(default=6379, description="Redis端口")
    REDIS_PASSWORD: SecretStr = Field(
        default=SecretStr(""), description="Redis密码"
    )
    REDIS_DB: int = Field(default=0, description="Redis数据库编号")
    REDIS_POOL_SIZE: int = Field(default=10, description="Redis连接池大小")
    REDIS_CONNECT_TIMEOUT: int = Field(
        default=5, description="Redis连接超时(秒)"
    )

    @property
    def REDIS_URL(self) -> str:
        """构造Redis连接URL。"""
        password = self.REDIS_PASSWORD.get_secret_value()
        if password:
            return f"redis://:{password}@{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"
        return f"redis://{self.REDIS_HOST}:{self.REDIS_PORT}/{self.REDIS_DB}"

    # ──────────────────────────────────────────────
    # ElasticSearch 8 (PRD 4.3: LBS地理空间搜索/全文检索)
    # ──────────────────────────────────────────────
    ES_HOST: str = Field(default="http://localhost:9200", description="ES连接地址")
    ES_USER: str = Field(default="elastic", description="ES用户名")
    ES_PASSWORD: SecretStr = Field(
        default=SecretStr("elastic123"), description="ES密码"
    )
    ES_INDEX_ORDER: str = Field(
        default="order_index", description="订单索引名称"
    )
    ES_REQUEST_TIMEOUT: int = Field(default=10, description="ES请求超时(秒)")

    # ──────────────────────────────────────────────
    # RabbitMQ 3.x (PRD 4.5/4.7/4.8/4.9: AI异步/通知/对账)
    # ──────────────────────────────────────────────
    MQ_HOST: str = Field(default="localhost", description="RabbitMQ主机名")
    MQ_PORT: int = Field(default=5672, description="RabbitMQ端口")
    MQ_USER: str = Field(default="halfcart", description="RabbitMQ用户名")
    MQ_PASSWORD: SecretStr = Field(
        default=SecretStr("halfcart123"), description="RabbitMQ密码"
    )
    MQ_VHOST: str = Field(default="/", description="RabbitMQ虚拟主机")
    MQ_EXCHANGE: str = Field(
        default="halfcart.exchange", description="Topic交换机名称"
    )
    MQ_RECONNECT_INTERVAL: int = Field(
        default=5, description="MQ重连间隔(秒)"
    )

    @property
    def RABBITMQ_URL(self) -> str:
        """构造RabbitMQ连接URL。"""
        password = self.MQ_PASSWORD.get_secret_value()
        return (
            f"amqp://{self.MQ_USER}:{password}"
            f"@{self.MQ_HOST}:{self.MQ_PORT}/{self.MQ_VHOST}"
        )

    # ──────────────────────────────────────────────
    # JWT 双Token (PRD 4.1: Access 7天 / Refresh 30天)
    # ──────────────────────────────────────────────
    JWT_SECRET_KEY: SecretStr = Field(
        default=SecretStr("change-this-secret-key-in-production"),
        description="JWT签名密钥",
    )
    JWT_ALGORITHM: str = Field(default="HS256", description="JWT签名算法")
    ACCESS_TOKEN_EXPIRE_SECONDS: int = Field(
        default=15 * 60, description="Access Token有效期(秒): 15分钟 (安全加固, 原PRD为7天)"
    )
    REFRESH_TOKEN_EXPIRE_SECONDS: int = Field(
        default=7 * 24 * 3600, description="Refresh Token有效期(秒): 7天 (安全加固, 原PRD为30天)"
    )
    JWT_TOKEN_ISSUER: str = Field(default="halfcart", description="JWT签发者")

    # ──────────────────────────────────────────────
    # AES 手机号加密 (PRD 5.3: 敏感数据存储加密)
    # ──────────────────────────────────────────────
    PHONE_ENCRYPTION_KEY: SecretStr = Field(
        default=SecretStr("halfcart-aes-key-32bytes!!!"),
        description="手机号AES加密密钥(32字节)",
    )

    # ──────────────────────────────────────────────
    # 短信服务商 (PRD 4.1: 验证码下发)
    # ──────────────────────────────────────────────
    SMS_PROVIDER: str = Field(default="aliyun", description="短信服务商")
    SMS_ACCESS_KEY: SecretStr = Field(
        default=SecretStr(""), description="短信服务商AccessKey"
    )
    SMS_SECRET_KEY: SecretStr = Field(
        default=SecretStr(""), description="短信服务商SecretKey"
    )
    SMS_ENDPOINT: str = Field(default="dypnsapi.aliyuncs.com", description="阿里云短信API端点")
    SMS_SIGN_NAME: str = Field(default="半仓智拼", description="短信签名")
    SMS_TEMPLATE_CODE: str = Field(default="", description="短信模板代码")
    SMS_CODE_LENGTH: int = Field(default=6, description="验证码长度")
    SMS_CODE_EXPIRE_SECONDS: int = Field(
        default=300, description="验证码有效期(秒): 5分钟 (PRD 4.1)"
    )
    SMS_CODE_RESEND_SECONDS: int = Field(
        default=60, description="验证码重发间隔(秒): 1分钟 (PRD 4.1)"
    )
    SMS_CODE_HOURLY_LIMIT: int = Field(
        default=5, description="同一手机号每小时最多发送次数 (PRD 4.1)"
    )

    # ──────────────────────────────────────────────
    # Dify AI 工作流 (PRD 4.7/4.8/4.9: NLP/OCR/纠纷裁判)
    # ──────────────────────────────────────────────
    DIFY_API_BASE: str = Field(
        default="https://api.dify.ai/v1", description="Dify API 地址"
    )
    DIFY_API_KEY: SecretStr = Field(
        default=SecretStr(""), description="Dify API Key"
    )
    DIFY_WORKFLOW_ASR: str = Field(
        default="asr-order-parser",
        description="[已废弃 v2026-07-22] 语音发单NLP工作流 — 已切至 DeepSeek (speech_service.py + deepseek_nlp.py). 后续版本移除.",
        deprecated=True,
    )
    # ── DeepSeek NLP 配置 (替代 Dify, PRD 4.7) ──
    DEEPSEEK_API_KEY: SecretStr = Field(
        default=SecretStr(""), description="DeepSeek API Key (https://platform.deepseek.com)"
    )
    DEEPSEEK_MODEL: str = Field(
        default="deepseek-chat", description="DeepSeek 模型名"
    )
    DEEPSEEK_API_BASE: str = Field(
        default="https://api.deepseek.com/v1", description="DeepSeek API 地址"
    )

    # ── 语音识别配置 (PRD 4.7 三通道) ──
    ALIYUN_NLS_APP_KEY: str = Field(
        default="", description="阿里云智能语音交互 AppKey"
    )
    ALIYUN_NLS_ACCESS_KEY_ID: str = Field(
        default="", description="阿里云 AccessKey ID (NLS)"
    )
    ALIYUN_NLS_ACCESS_KEY_SECRET: str = Field(
        default="", description="阿里云 AccessKey Secret (NLS)"
    )
    SPEECH_ENGINE: str = Field(
        default="auto", description="语音引擎: auto(自动选择) / aliyun / whisper / webspeech"
    )
    WHISPER_MODEL: str = Field(
        default="tiny", description="Whisper 模型大小: tiny / base / small"
    )
    DIFY_WORKFLOW_OCR: str = Field(
        default="ocr-receipt-parser", description="小票OCR工作流名称"
    )
    DIFY_WORKFLOW_DISPUTE: str = Field(
        default="dispute-judge", description="纠纷裁判工作流名称"
    )
    DIFY_TIMEOUT_SECONDS: int = Field(
        default=120, description="Dify API请求超时(秒) — OCR需较长时间"
    )

    # ── Dify 工作流独立配置 (PRD 4.7 扩展) ──
    # 拼单文本解析工作流
    DIFY_TEXT_PARSER_API_KEY: SecretStr = Field(
        default=SecretStr(""), description="拼单文本解析工作流 API Key"
    )
    DIFY_TEXT_PARSER_WORKFLOW: str = Field(
        default="text-order-parser", description="拼单文本解析工作流名称"
    )
    DIFY_TEXT_PARSER_INPUT_VAR: str = Field(
        default="user_input_text", description="拼单文本解析 Dify inputs 变量名"
    )
    DIFY_TEXT_PARSER_THRESHOLD: float = Field(
        default=0.6, description="拼单文本解析置信度阈值"
    )

    # ── Dify 工作流2: 小票OCR识别 (PRD 4.8) ──
    DIFY_OCR_API_KEY: SecretStr = Field(
        default=SecretStr(""), description="小票OCR工作流 API Key"
    )
    DIFY_OCR_WORKFLOW: str = Field(
        default="ocr-receipt-parser", description="小票OCR工作流名称"
    )
    DIFY_OCR_INPUT_VAR: str = Field(
        default="receipt_image", description="小票OCR inputs 变量名"
    )
    DIFY_OCR_THRESHOLD: float = Field(
        default=0.75, description="OCR置信度阈值 (PRD 4.8: 75%)"
    )

    # ── Dify 工作流3: 纠纷多模态裁判 (PRD 4.9) ──
    DIFY_DISPUTE_API_KEY: SecretStr = Field(
        default=SecretStr(""), description="纠纷裁判工作流 API Key"
    )
    DIFY_DISPUTE_WORKFLOW: str = Field(
        default="dispute-judge", description="纠纷裁判工作流名称"
    )
    DIFY_DISPUTE_INPUT_VAR: str = Field(
        default="description", description="纠纷裁判 inputs 主变量名"
    )
    DIFY_DISPUTE_THRESHOLD: float = Field(
        default=0.80, description="纠纷裁判置信度阈值 (PRD 4.9: 80%)"
    )

    # ──────────────────────────────────────────────
    # Mock 支付开关 (PRD 5.4: MVP阶段Mock支付)
    # ──────────────────────────────────────────────
    MOCK_PAYMENT: bool = Field(
        default=True, description="Mock支付模式 (PRD 5.4: 开启后直通支付/核销)"
    )

    TEST_MODE: bool = Field(
        default=False, description="测试模式 (开启 /api/v1/test/* 接口; 生产环境必须关闭; ⚠️ 禁止在生产环境开启)"
    )

    # ──────────────────────────────────────────────
    # 业务规则常量 (PRD 4.3/4.5/4.6/4.10)
    # ──────────────────────────────────────────────
    LBS_SEARCH_RADIUS_KM: float = Field(
        default=1.5, description="LBS雷达匹配半径(km) (PRD 4.3)"
    )
    LBS_CACHE_TTL_SECONDS: int = Field(
        default=10, description="LBS雷达缓存刷新间隔(秒) (PRD 4.3防刷规则)"
    )
    PLATFORM_SERVICE_FEE_RATE: float = Field(
        default=0.03, description="平台服务费率: 3% (PRD 4.5)"
    )
    VERIFICATION_CODE_LOCK_THRESHOLD: int = Field(
        default=5, description="核销码连续错误锁定阈值 (PRD 4.5)"
    )
    VERIFICATION_CODE_LOCK_MINUTES: int = Field(
        default=10, description="核销码锁定时间(分钟) (PRD 4.5)"
    )
    WITHDRAW_MIN_AMOUNT: float = Field(
        default=10.0, description="最低提现金额(元) (PRD 4.10)"
    )
    DISPUTE_AUTO_JUDGE_MAX_AMOUNT: float = Field(
        default=500.0, description="AI自动裁判最高金额(元) (PRD 4.9)"
    )
    DISPUTE_JUDGE_CONFIDENCE_THRESHOLD: float = Field(
        default=0.80, description="AI判责置信度阈值 (PRD 4.9)"
    )
    CHAT_CLEANUP_HOURS: int = Field(
        default=24, description="订单完成后销毁聊天记录小时数 (PRD 3.1)"
    )
    RECONCILE_SCAN_HOURS: int = Field(
        default=24, description="增量对账扫描近N小时活跃订单 (PRD 4.5)"
    )
    RECONCILE_CRON_INTERVAL_SECONDS: int = Field(
        default=3600, description="对账任务间隔(秒): 每小时 (PRD 4.5)"
    )
    ORDER_GATHERING_TIMEOUT_SECONDS: int = Field(
        default=86400, description="拼单默认截单时间(秒): 24h"
    )
    SPOT_ORDER_VALID_HOURS: int = Field(
        default=24, description="现货订单默认有效期(小时) (PRD 4.8)"
    )
    SPOT_MAX_SPLIT_COUNT: int = Field(
        default=5, description="单张小票最多拆分现货子订单数 (PRD 4.8)"
    )

    # ──────────────────────────────────────────────
    # 文件上传
    # ──────────────────────────────────────────────
    MAX_AUDIO_SIZE_BYTES: int = Field(
        default=1 * 1024 * 1024, description="音频文件最大1MB (PRD 4.7)"
    )
    MAX_IMAGE_SIZE_BYTES: int = Field(
        default=2 * 1024 * 1024, description="图片文件最大2MB (PRD 4.8)"
    )
    UPLOAD_RETENTION_DAYS: int = Field(
        default=7, description="上传文件保留天数 (PRD 5.3)"
    )

    # ──────────────────────────────────────────────
    # 微信公众号 (服务器校验)
    # ──────────────────────────────────────────────
    WECHAT_MP_TOKEN: str = Field(
        default="half", description="微信公众号 Token (消息接口配置验证)"
    )
    WECHAT_MP_APPID: str = Field(
        default="", description="微信公众号 AppID"
    )
    WECHAT_MP_SECRET: SecretStr = Field(
        default=SecretStr(""), description="微信公众号 AppSecret"
    )

    # ──────────────────────────────────────────────
    # 微信开放平台 / 公众号网页授权 (PRD 4.1 扩展: 扫码登录)
    # ──────────────────────────────────────────────
    WECHAT_APP_ID: str = Field(default="", description="微信 AppID")
    WECHAT_APP_SECRET: SecretStr = Field(
        default=SecretStr(""), description="微信 AppSecret"
    )
    WECHAT_REDIRECT_URI: str = Field(
        default="http://127.0.0.1:8080/api/v1/auth/wechat/callback",
        description="微信 OAuth 回调地址 (必须与微信后台配置一致)",
    )
    WECHAT_SCOPE: str = Field(
        default="snsapi_userinfo",
        description="公众号网页授权 scope: snsapi_base(静默) / snsapi_userinfo(需用户同意)",
    )
    WECHAT_FRONTEND_URL: str = Field(
        default="http://127.0.0.1:3000",
        description="前端地址, 用于 OAuth 回调后的 302 跳转",
    )

    # ──────────────────────────────────────────────
    # 高德地图 LBS (PRD 4.3: 雷达/反欺诈/地址解析)
    # ──────────────────────────────────────────────
    AMAP_ENABLE: bool = Field(
        default=True, description="高德能力总开关"
    )
    AMAP_MOCK_ENABLED: bool = Field(
        default=True, description="Mock 模式: true=模拟数据 false=真实API"
    )
    AMAP_WEB_KEY: SecretStr = Field(
        default=SecretStr(""), description="高德 Web 服务 Key"
    )
    AMAP_COORD_TYPE: str = Field(
        default="gcj02", description="统一坐标系 (gcj02/wgs84)"
    )
    AMAP_TIMEOUT: int = Field(
        default=3, description="高德接口超时(秒)"
    )
    AMAP_RETRY_TIMES: int = Field(
        default=1, description="失败自动重试次数"
    )
    AMAP_CACHE_TTL: int = Field(
        default=86400, description="地址解析结果缓存时长(秒), 默认24h"
    )
    AMAP_FRAUD_SPEED_LIMIT: int = Field(
        default=300, description="反欺诈位移速度阈值(km/h)"
    )
    AMAP_FRAUD_ACCURACY_THRESHOLD: int = Field(
        default=500, description="定位精度阈值(米),低于该值降权"
    )
    AMAP_RADIUS_LIMIT: int = Field(
        default=1500, description="雷达匹配半径(米)"
    )
    AMAP_CIRCUIT_BREAKER_THRESHOLD: int = Field(
        default=10, description="熔断器连续失败触发阈值"
    )
    AMAP_CIRCUIT_BREAKER_COOLDOWN: int = Field(
        default=300, description="熔断器冷却时间(秒)"
    )

    # ──────────────────────────────────────────────
    # 支付宝支付 (PRD V3.2: 沙箱/正式环境)
    # ──────────────────────────────────────────────
    ALIPAY_APP_ID: str = Field(
        default="", description="支付宝应用ID (沙箱/正式)"
    )
    ALIPAY_PRIVATE_KEY: SecretStr = Field(
        default=SecretStr(""), description="应用私钥 (PKCS1/PKCS8 PEM)"
    )
    ALIPAY_PUBLIC_KEY: SecretStr = Field(
        default=SecretStr(""), description="支付宝公钥"
    )
    ALIPAY_GATEWAY: str = Field(
        default="https://openapi-sandbox.dl.alipaydev.com/gateway.do",
        description="支付宝网关: 沙箱=sandbox.dl.alipaydev.com / 正式=openapi.alipay.com",
    )
    ALIPAY_SANDBOX: bool = Field(
        default=True, description="沙箱模式: true=沙箱 false=正式"
    )
    ALIPAY_NOTIFY_URL: str = Field(
        default="", description="支付宝异步通知回调地址 (公网可访问)"
    )
    ALIPAY_RETURN_URL: str = Field(
        default="", description="支付宝同步回调地址 (前端跳转)"
    )

    # ──────────────────────────────────────────────
    # 限流 (PRD 5.1)
    # ──────────────────────────────────────────────
    RATE_LIMIT_GLOBAL: str = Field(
        default="100/minute", description="全局接口限流"
    )
    RATE_LIMIT_AUTH: str = Field(
        default="5/minute", description="登录接口限流"
    )
    RATE_LIMIT_ORDER_JOIN: str = Field(
        default="10/second", description="加入拼单接口限流"
    )


@lru_cache()
def get_settings() -> Settings:
    """获取配置单例，使用 lru_cache 避免重复实例化。"""
    return Settings()


# 全局配置实例（便捷导入）
settings = get_settings()
