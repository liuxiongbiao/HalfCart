-- ===========================================================
-- HalfCart V3.2 补全索引脚本
-- ===========================================================
-- 执行方式: mysql -u halfcart -p halfcart < v3.2_indexes.sql
-- 所有索引均使用 IF NOT EXISTS 语法避免重复执行报错

-- 1. t_users: 信用分排序索引 (雷达信用分加权)
ALTER TABLE t_users ADD INDEX IF NOT EXISTS idx_credit_score (credit_score);

-- 2. t_orders: 现货过滤索引
ALTER TABLE t_orders ADD INDEX IF NOT EXISTS idx_is_spot_status (is_spot, status, spot_valid_until);

-- 3. t_orders: 超时解散扫描索引
ALTER TABLE t_orders ADD INDEX IF NOT EXISTS idx_status_deadline (status, deadline);

-- 4. t_orders: 待交接超时告警索引
ALTER TABLE t_orders ADD INDEX IF NOT EXISTS idx_status_updated (status, updated_at);

-- 5. t_order_participants: 支付状态过滤索引
ALTER TABLE t_order_participants ADD INDEX IF NOT EXISTS idx_pay_status (pay_status, order_id);

-- 6. t_withdraw_applications: 待处理提现检查索引
ALTER TABLE t_withdraw_applications ADD INDEX IF NOT EXISTS idx_user_status (user_id, status, created_at);

-- 7. t_chat_messages: 阅后即焚清理索引
ALTER TABLE t_chat_messages ADD INDEX IF NOT EXISTS idx_order_created (order_id, created_at);

-- 8. t_payment_logs: 对账扫描索引
ALTER TABLE t_payment_logs ADD INDEX IF NOT EXISTS idx_user_created_status (user_id, created_at, status);

-- 9. t_alipay_records: 退款关联查询索引
ALTER TABLE t_alipay_records ADD INDEX IF NOT EXISTS idx_order_user_status (order_id, user_id, trade_status);
