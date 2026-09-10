-- ============================================================
-- 敏感字段加密 — 扩展列长度以容纳 AES-256-CBC 密文
-- 创建日期: 2026-07-23
-- ============================================================

ALTER TABLE t_user_addresses
    MODIFY COLUMN phone VARCHAR(256) NOT NULL COMMENT '手机号 (AES-256-CBC加密)';

ALTER TABLE t_user_payment_accounts
    MODIFY COLUMN real_name VARCHAR(512) NOT NULL COMMENT '收款人姓名 (AES-256-CBC加密)',
    MODIFY COLUMN account VARCHAR(512) NOT NULL COMMENT '收款账号 (AES-256-CBC加密)';
