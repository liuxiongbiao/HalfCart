-- ===========================================================
-- 半仓智拼 (HalfCart) 品类字典表 + 种子数据
-- ===========================================================

CREATE TABLE IF NOT EXISTS `categories` (
    `id`         INT PRIMARY KEY AUTO_INCREMENT,
    `key`        VARCHAR(30)  NOT NULL COMMENT '品类标识(food/fresh/daily/snack/drink)',
    `label`      VARCHAR(30)  NOT NULL COMMENT '品类名称(美食/生鲜/日用/零食/饮品)',
    `icon`       VARCHAR(10)  NOT NULL DEFAULT '' COMMENT '图标emoji',
    `sort_order` INT          NOT NULL DEFAULT 0 COMMENT '排序权重(越小越前)',
    `is_active`  TINYINT(1)   NOT NULL DEFAULT 1 COMMENT '是否启用: 1=启用 0=禁用',
    `created_at` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
    `updated_at` DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    UNIQUE KEY `uk_key` (`key`)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci COMMENT='商品品类字典';

-- 种子数据
INSERT IGNORE INTO `categories` (`key`, `label`, `icon`, `sort_order`) VALUES
    ('food',  '美食', '🍔', 1),
    ('fresh', '生鲜', '🥬', 2),
    ('daily', '日用', '📦', 3),
    ('snack', '零食', '🍿', 4),
    ('drink', '饮品', '🧃', 5);
