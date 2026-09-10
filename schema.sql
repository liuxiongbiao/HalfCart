-- ===========================================================
-- 半仓智拼 (HalfCart) 数据库建表 SQL
-- ⚠️ 注意: deploy/mysql/init/ 目录下的按序号命名的迁移文件为权威版本。
--    本文件为初始版本，可能与最新迁移不同步，仅供参考。
-- ===========================================================
-- 版本: V1.1 — 新增 t_sms_logs

-- ═══════════════════════════════════════════════════
-- 11. 短信发送日志表 — t_sms_logs
-- ═══════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS t_sms_logs (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '日志ID',
    phone           VARCHAR(128)   NOT NULL                   COMMENT '手机号 (加密存储)',
    scene           VARCHAR(32)    NOT NULL                   COMMENT '场景: login/withdraw/notice',
    template_code   VARCHAR(32)    NOT NULL                   COMMENT '阿里云模板CODE',
    status          TINYINT        NOT NULL DEFAULT 1          COMMENT '1-成功 0-失败',
    error_msg       VARCHAR(500)   NOT NULL DEFAULT ''         COMMENT '失败原因',
    duration_ms     INT            NOT NULL DEFAULT 0          COMMENT '耗时ms',
    out_id          VARCHAR(64)    NOT NULL DEFAULT ''         COMMENT '外部流水号',
    created_at      DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='短信发送日志表';

-- ===========================================================
-- 半仓智拼 (HalfCart) 数据库建表 SQL (主表)
-- ===========================================================
-- 版本: V1.0
-- 引擎: MySQL 8.0+ (InnoDB, utf8mb4)
-- 100% 对齐 ORM 模型定义 (models/*.py)
-- ===========================================================

CREATE DATABASE IF NOT EXISTS halfcart
    DEFAULT CHARACTER SET utf8mb4
    DEFAULT COLLATE utf8mb4_unicode_ci;

USE halfcart;

-- ═══════════════════════════════════════════════════
-- 1. 用户表 — t_users (底座设计 2.2)
-- ═══════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS t_users (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '用户ID',
    phone           VARCHAR(128)   NOT NULL UNIQUE            COMMENT '手机号 AES-256-CBC 加密存储',
    phone_hash      CHAR(64)       NOT NULL UNIQUE            COMMENT '手机号 SHA256 哈希，快速查找索引',
    username        VARCHAR(32)    DEFAULT NULL UNIQUE        COMMENT '用户账号 (字母+数字, 4-32位)',
    password_hash   VARCHAR(128)   DEFAULT NULL               COMMENT '密码哈希值 HMAC-SHA256',
    wechat_openid   VARCHAR(64)    DEFAULT NULL UNIQUE        COMMENT '微信 OpenID，扫码登录绑定',
    nickname        VARCHAR(30)    NOT NULL DEFAULT ''        COMMENT '用户昵称',
    avatar_url      VARCHAR(255)   NOT NULL DEFAULT ''        COMMENT '头像URL',
    credit_score    TINYINT UNSIGNED NOT NULL DEFAULT 100     COMMENT '信用分 0-120，初始100分',
    wallet_balance  DECIMAL(12,2)  NOT NULL DEFAULT 0.00     COMMENT '可提现钱包余额 — 核销后扣除服务费的资金 (PRD 3.2)',
    frozen_balance  DECIMAL(12,2)  NOT NULL DEFAULT 0.00     COMMENT '冻结担保金 — 拼友支付后平台托管 (PRD 3.2)',
    role            TINYINT        NOT NULL DEFAULT 0         COMMENT '0-普通用户 1-管理员',
    status          TINYINT        NOT NULL DEFAULT 1         COMMENT '1-正常 0-封禁',
    last_login_at   DATETIME       DEFAULT NULL               COMMENT '最后登录时间',
    created_at      DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE INDEX uk_phone_hash (phone_hash),
    UNIQUE INDEX uk_username (username),
    UNIQUE INDEX uk_wechat_openid (wechat_openid),
    INDEX        idx_created_at (created_at),
    INDEX        idx_wallet_balance (wallet_balance),
    INDEX        idx_frozen_balance (frozen_balance),
    INDEX        idx_status (status),
    INDEX        idx_updated_at (updated_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户表';

-- ═══════════════════════════════════════════════════
-- 2. 订单表 — t_orders (底座设计 2.3)
-- ═══════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS t_orders (
    id               BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '订单ID',
    order_no         CHAR(22)       NOT NULL UNIQUE            COMMENT '业务单号 HC+时间戳+随机串',
    creator_id       BIGINT UNSIGNED NOT NULL                  COMMENT '团长用户ID',
    status           TINYINT        NOT NULL DEFAULT 0         COMMENT '0-集资中 1-采购中 2-待交接 3-已完成 4-已解散',
    order_type       TINYINT        NOT NULL DEFAULT 1         COMMENT '1-普通拼单 2-现货转让',
    goods_name       VARCHAR(100)   NOT NULL                   COMMENT '商品名称',
    goods_desc       VARCHAR(500)   NOT NULL DEFAULT ''        COMMENT '商品描述',
    goods_category   VARCHAR(30)    NOT NULL DEFAULT ''        COMMENT '商品品类',
    purchase_channel VARCHAR(50)    NOT NULL DEFAULT ''        COMMENT '采购渠道',
    total_people     SMALLINT UNSIGNED NOT NULL                COMMENT '需要总人数, 含团长 ≥2',
    current_people   SMALLINT UNSIGNED NOT NULL DEFAULT 1      COMMENT '已加入人数',
    price_per_person DECIMAL(10,2)  NOT NULL                   COMMENT '人均价格',
    total_amount     DECIMAL(12,2)  NOT NULL                   COMMENT '订单总金额 = price_per_person × (total_people - 1)',
    meet_location    VARCHAR(200)   NOT NULL                   COMMENT '交接地点',
    location_lat     DECIMAL(10,7)  NOT NULL                   COMMENT '纬度',
    location_lng     DECIMAL(10,7)  NOT NULL                   COMMENT '经度',
    deadline         DATETIME       NOT NULL                   COMMENT '截单时间',
    receipt_img      VARCHAR(255)   NOT NULL DEFAULT ''        COMMENT '小票原图URL (现货转让)',
    original_price   DECIMAL(10,2)  DEFAULT NULL               COMMENT '小票原价',
    is_spot          TINYINT        NOT NULL DEFAULT 0         COMMENT '现货标记 0-否 1-是',
    spot_valid_until DATETIME       DEFAULT NULL               COMMENT '现货有效期 (默认24h)',
    dispute_status   TINYINT        NOT NULL DEFAULT 0         COMMENT '0-无纠纷 1-处理中 2-已结案',
    finished_at      DATETIME       DEFAULT NULL               COMMENT '核销完成时间',
    created_at       DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE INDEX uk_order_no (order_no),
    INDEX        idx_creator_id (creator_id),
    INDEX        idx_status (status),
    INDEX        idx_deadline (deadline),
    INDEX        idx_goods_category (goods_category),
    INDEX        idx_updated_at (updated_at),
    INDEX        idx_status_creator (status, creator_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='订单表';

-- ═══════════════════════════════════════════════════
-- 3. 订单参与人表 — t_order_participants (底座设计 2.4)
-- ═══════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS t_order_participants (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '参与记录ID',
    order_id        BIGINT UNSIGNED NOT NULL                   COMMENT '订单ID',
    user_id         BIGINT UNSIGNED NOT NULL                   COMMENT '用户ID',
    role            TINYINT        NOT NULL                    COMMENT '1-团长 2-拼友',
    pay_status      TINYINT        NOT NULL DEFAULT 0          COMMENT '0-待支付 1-已支付(资金冻结) 2-已退款 3-已结算',
    pay_amount      DECIMAL(10,2)  NOT NULL                   COMMENT '实付金额',
    refund_amount   DECIMAL(10,2)  DEFAULT NULL                COMMENT '退款金额',
    refund_ratio    DECIMAL(5,4)   DEFAULT NULL                COMMENT '退款比例 (1.0=全额)',
    refund_reason   VARCHAR(200)   NOT NULL DEFAULT ''         COMMENT '退款原因',
    refunded_at     DATETIME       DEFAULT NULL                COMMENT '退款时间',
    joined_at       DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '加入时间',
    created_at      DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE INDEX uk_order_user (order_id, user_id),
    INDEX        idx_user_id (user_id),
    INDEX        idx_user_id_status (user_id, pay_status),
    INDEX        idx_order_id_role (order_id, role)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='订单参与人表';

-- ═══════════════════════════════════════════════════
-- 4. 资金流水表 — t_payment_logs (底座设计 2.5)
-- ═══════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS t_payment_logs (
    id                   BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '流水ID',
    log_no               CHAR(26)       NOT NULL UNIQUE         COMMENT '流水单号 全局唯一',
    user_id              BIGINT UNSIGNED NOT NULL                COMMENT '用户ID',
    order_id             BIGINT UNSIGNED DEFAULT NULL            COMMENT '关联订单ID (提现流水不关联订单)',
    type                 TINYINT        NOT NULL                 COMMENT '1-支付冻结 2-退款解冻 3-核销结算 4-违约金收入 5-服务费扣除 6-提现冻结 7-提现打款 8-提现退回 9-纠纷退款',
    amount               DECIMAL(12,2)  NOT NULL                 COMMENT '变动金额 (正=入账, 负=出账)',
    balance_before       DECIMAL(12,2)  NOT NULL                 COMMENT '变动前余额',
    balance_after        DECIMAL(12,2)  NOT NULL                 COMMENT '变动后余额',
    target_balance_type  TINYINT        NOT NULL                 COMMENT '1-wallet_balance 2-frozen_balance',
    status               TINYINT        NOT NULL DEFAULT 1       COMMENT '1-成功 0-失败(事务回滚)',
    remark               VARCHAR(255)   NOT NULL DEFAULT ''      COMMENT '备注',
    idempotent_key       VARCHAR(80)    NOT NULL UNIQUE          COMMENT '幂等键 (SHA256+后缀) — PRD 5.3 幂等防护核心',
    created_at           DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at           DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE INDEX uk_log_no (log_no),
    UNIQUE INDEX uk_idempotent_key (idempotent_key),
    INDEX        idx_user_id (user_id),
    INDEX        idx_order_id (order_id),
    INDEX        idx_created_at (created_at),
    INDEX        idx_user_created (user_id, created_at),
    INDEX        idx_type_created (type, created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='资金流水表';

-- ═══════════════════════════════════════════════════
-- 5. 核销码表 — t_verification_codes (底座设计 2.6)
-- ═══════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS t_verification_codes (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '记录ID',
    order_id        BIGINT UNSIGNED NOT NULL UNIQUE            COMMENT '订单ID (一对一)',
    code            CHAR(6)         NOT NULL                   COMMENT '6位数字核销码',
    is_used         TINYINT         NOT NULL DEFAULT 0         COMMENT '0-未使用 1-已使用',
    error_count     TINYINT UNSIGNED NOT NULL DEFAULT 0        COMMENT '连续错误次数 ≥5锁定10min',
    locked_until    DATETIME        DEFAULT NULL               COMMENT '锁定截止时间',
    used_at         DATETIME        DEFAULT NULL               COMMENT '核销时间',
    created_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME        NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE INDEX uk_order_id (order_id),
    INDEX        idx_code (code)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='核销码表';

-- ═══════════════════════════════════════════════════
-- 6. 提现申请表 — t_withdraw_applications (底座设计 2.7)
-- ═══════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS t_withdraw_applications (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '提现记录ID',
    withdraw_no     CHAR(22)       NOT NULL UNIQUE            COMMENT '提现单号',
    user_id         BIGINT UNSIGNED NOT NULL                  COMMENT '用户ID',
    amount          DECIMAL(10,2)  NOT NULL                   COMMENT '提现金额 ≥10 ≤5000',
    pay_method      TINYINT        NOT NULL                   COMMENT '收款方式 1-微信 2-支付宝',
    pay_account     VARCHAR(255)   NOT NULL                   COMMENT '收款账号 (加密存储)',
    real_name       VARCHAR(100)   NOT NULL                   COMMENT '收款人姓名 (加密存储)',
    status          TINYINT        NOT NULL DEFAULT 0          COMMENT '0-待审核 1-已打款 2-审核拒绝 3-打款失败',
    audit_remark    VARCHAR(255)   NOT NULL DEFAULT ''         COMMENT '审核备注',
    audited_at      DATETIME       DEFAULT NULL               COMMENT '审核时间',
    created_at      DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE INDEX uk_withdraw_no (withdraw_no),
    INDEX        idx_user_id (user_id),
    INDEX        idx_status (status),
    INDEX        idx_user_status (user_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='提现申请表';

-- ═══════════════════════════════════════════════════
-- 7. 聊天消息表 — t_chat_messages (底座设计 2.8)
-- ═══════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS t_chat_messages (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '消息ID',
    order_id        BIGINT UNSIGNED NOT NULL                  COMMENT '订单ID',
    sender_id       BIGINT UNSIGNED NOT NULL                  COMMENT '发送者ID',
    receiver_id     BIGINT UNSIGNED NOT NULL                  COMMENT '接收者ID',
    message_type    TINYINT        NOT NULL DEFAULT 1          COMMENT '1-用户文本 2-系统通知',
    content         VARCHAR(2000)  NOT NULL                   COMMENT '消息内容',
    is_read         TINYINT        NOT NULL DEFAULT 0          COMMENT '0-未读 1-已读',
    business_id     VARCHAR(64)    NOT NULL DEFAULT ''         COMMENT '幂等键: 关联业务ID',
    created_at      DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_order_id_created (order_id, created_at),
    INDEX idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='聊天消息表 — 订单完成24h后阅后即焚';

-- ═══════════════════════════════════════════════════
-- 8. 纠纷记录表 — t_disputes (底座设计 2.9)
-- ═══════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS t_disputes (
    id                   BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '纠纷ID',
    order_id             BIGINT UNSIGNED NOT NULL              COMMENT '订单ID',
    applicant_id         BIGINT UNSIGNED NOT NULL              COMMENT '发起人用户ID',
    dispute_type         TINYINT        NOT NULL               COMMENT '纠纷类型: 1-商品破损 2-货不对板 3-质量问题 9-其他',
    description          VARCHAR(2000)  NOT NULL               COMMENT '纠纷描述',
    evidence_images      VARCHAR(2000)  NOT NULL DEFAULT ''    COMMENT '凭证图片URL数组JSON',
    status               TINYINT        NOT NULL DEFAULT 0     COMMENT '0-处理中 1-已完成 2-待人工 3-已关闭',
    ai_responsible_party VARCHAR(32)    NOT NULL DEFAULT ''    COMMENT 'AI判责: creator/participant/shared',
    ai_creator_ratio     FLOAT          NOT NULL DEFAULT 0.0   COMMENT '团长责任比例',
    ai_participant_ratio FLOAT          NOT NULL DEFAULT 0.0   COMMENT '拼友责任比例',
    ai_confidence        FLOAT          NOT NULL DEFAULT 0.0   COMMENT 'AI置信度 0.0-1.0',
    ai_reason            VARCHAR(2000)  NOT NULL DEFAULT ''    COMMENT '判责理由',
    resolved_at          DATETIME       DEFAULT NULL           COMMENT '处理完成时间',
    created_at           DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at           DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_order_id (order_id),
    INDEX idx_applicant_id (applicant_id),
    INDEX idx_status (status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='纠纷记录表';

-- ═══════════════════════════════════════════════════
-- 9. 对账执行记录表 — t_reconciliation_records
-- ═══════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS t_reconciliation_records (
    id               BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '对账记录ID',
    recon_type       VARCHAR(16)    NOT NULL                   COMMENT 'incremental / full',
    cycle_start      DATETIME       NOT NULL                   COMMENT '对账周期开始',
    cycle_end        DATETIME       NOT NULL                   COMMENT '对账周期结束',
    orders_checked   INT            NOT NULL DEFAULT 0         COMMENT '检测订单数',
    users_checked    INT            NOT NULL DEFAULT 0         COMMENT '检测用户数',
    anomalies_total  INT            NOT NULL DEFAULT 0         COMMENT '异常总数',
    auto_fixed       INT            NOT NULL DEFAULT 0         COMMENT '已自动补偿数',
    pending_manual   INT            NOT NULL DEFAULT 0         COMMENT '待人工处理数',
    status           VARCHAR(16)    NOT NULL DEFAULT 'completed' COMMENT '执行状态',
    duration_ms      INT            NOT NULL DEFAULT 0         COMMENT '执行耗时ms',
    idempotent_key   VARCHAR(64)    NOT NULL UNIQUE            COMMENT '幂等键: recon:{type}:{start}:{end}',
    created_at       DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE INDEX uk_idempotent_key (idempotent_key),
    INDEX        idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='对账执行记录表';

-- ═══════════════════════════════════════════════════
-- 10. 对账异常明细表 — t_reconciliation_anomalies
-- ═══════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS t_reconciliation_anomalies (
    id               BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '异常ID',
    record_id        BIGINT UNSIGNED NOT NULL                  COMMENT '对账记录ID',
    anomaly_type     VARCHAR(32)    NOT NULL                   COMMENT 'balance_mismatch / payment_gap / status_conflict',
    order_id         BIGINT UNSIGNED DEFAULT NULL              COMMENT '关联订单ID',
    user_id          BIGINT UNSIGNED DEFAULT NULL              COMMENT '关联用户ID',
    description      VARCHAR(1000)  NOT NULL DEFAULT ''        COMMENT '异常描述',
    fix_status       VARCHAR(16)    NOT NULL DEFAULT 'pending' COMMENT 'pending / auto_fixed / manual_required / resolved',
    fix_remark       VARCHAR(500)   NOT NULL DEFAULT ''        COMMENT '处理备注',
    expected_value   VARCHAR(200)   NOT NULL DEFAULT ''        COMMENT '期望值',
    actual_value     VARCHAR(200)   NOT NULL DEFAULT ''        COMMENT '实际值',
    created_at       DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at       DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    INDEX idx_record_id (record_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='对账异常明细表';

-- ═══════════════════════════════════════════════════
-- 12. 支付宝支付流水表 — t_alipay_records
-- ═══════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS t_alipay_records (
    id              BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '流水ID',
    out_trade_no    CHAR(32)       NOT NULL UNIQUE             COMMENT '商户订单号 HC+时间戳+随机串',
    trade_no        VARCHAR(64)    DEFAULT NULL UNIQUE         COMMENT '支付宝交易号 (支付成功后回写)',
    user_id         BIGINT UNSIGNED NOT NULL                  COMMENT '支付用户ID',
    order_id        BIGINT UNSIGNED NOT NULL                  COMMENT '关联订单ID',
    amount          DECIMAL(12,2)  NOT NULL                   COMMENT '支付金额 (元)',
    refund_amount   DECIMAL(12,2)  DEFAULT NULL               COMMENT '退款金额 (元)',
    subject         VARCHAR(256)   NOT NULL                   COMMENT '商品标题',
    body            VARCHAR(500)   NOT NULL DEFAULT ''         COMMENT '商品描述',
    trade_status    TINYINT        NOT NULL DEFAULT 0          COMMENT '0-待支付 1-成功 2-已关闭 3-全额退款 4-部分退款',
    pay_time        DATETIME       DEFAULT NULL               COMMENT '支付宝支付时间',
    notify_time     DATETIME       DEFAULT NULL               COMMENT '异步回调到达时间',
    refund_reason   VARCHAR(500)   DEFAULT NULL               COMMENT '退款原因',
    raw_notify      TEXT           DEFAULT NULL               COMMENT '原始回调JSON (对账排错)',
    created_at      DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at      DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,

    UNIQUE INDEX uk_out_trade_no (out_trade_no),
    UNIQUE INDEX uk_trade_no (trade_no),
    INDEX        idx_user_id (user_id),
    INDEX        idx_order_id (order_id),
    INDEX        idx_trade_status (trade_status),
    INDEX        idx_created_at (created_at)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='支付宝支付流水表';

-- ═══════════════════════════════════════════════════
-- 13. 用户收货地址表 — t_user_addresses
-- ═══════════════════════════════════════════════════

CREATE TABLE IF NOT EXISTS t_user_addresses (
    id          BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY COMMENT '地址ID',
    user_id     BIGINT UNSIGNED NOT NULL            COMMENT '用户ID',
    name        VARCHAR(20)    NOT NULL              COMMENT '收货人姓名',
    phone       VARCHAR(11)    NOT NULL              COMMENT '手机号',
    province    VARCHAR(20)    NOT NULL              COMMENT '省份',
    city        VARCHAR(20)    NOT NULL              COMMENT '城市',
    district    VARCHAR(20)    NOT NULL              COMMENT '区县',
    detail      VARCHAR(100)   NOT NULL              COMMENT '详细地址',
    is_default  TINYINT        NOT NULL DEFAULT 0    COMMENT '0-普通 1-默认',
    created_at  DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at  DATETIME       NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_user_id (user_id),
    INDEX idx_is_default (user_id, is_default)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户收货地址表';
