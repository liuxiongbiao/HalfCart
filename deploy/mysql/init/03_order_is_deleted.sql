-- ===========================================================
-- 订单软删除字段
-- 2026-07-22: 新增 is_deleted 列，支持订单删除功能
-- ===========================================================
ALTER TABLE t_orders ADD COLUMN is_deleted TINYINT NOT NULL DEFAULT 0 COMMENT '软删除 0-否 1-是' AFTER finished_at;
