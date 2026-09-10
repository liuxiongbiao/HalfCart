-- ============================================================
-- 收款账户表 (t_user_payment_accounts)
-- 创建日期: 2026-07-23
-- 说明: 用户支付宝/微信收款账户管理, 支持默认账户
-- ============================================================

CREATE TABLE IF NOT EXISTS t_user_payment_accounts (
    id           BIGINT UNSIGNED AUTO_INCREMENT PRIMARY KEY,
    user_id      BIGINT UNSIGNED NOT NULL COMMENT '用户ID',
    account_type TINYINT NOT NULL COMMENT '收款方式: 1-微信 2-支付宝',
    real_name    VARCHAR(100) NOT NULL COMMENT '收款人姓名',
    account      VARCHAR(255) NOT NULL COMMENT '收款账号',
    is_default   TINYINT NOT NULL DEFAULT 0 COMMENT '是否默认: 0-否 1-是',
    created_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at   DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    INDEX idx_user_id (user_id),
    INDEX idx_user_default (user_id, is_default)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='用户收款账户表';
