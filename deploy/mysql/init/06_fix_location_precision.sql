-- ============================================================
-- 修复 location_lat / location_lng 列精度
-- ============================================================
-- 原定义为 DECIMAL(10,0) (零位小数), 导致坐标被截断为整数
-- 改为 DECIMAL(10,7) (7位小数), 精确到 ~1cm 级别
-- 创建日期: 2026-07-24
-- ============================================================

ALTER TABLE t_orders MODIFY location_lat DECIMAL(10,7) NOT NULL COMMENT '纬度';
ALTER TABLE t_orders MODIFY location_lng DECIMAL(10,7) NOT NULL COMMENT '经度';
