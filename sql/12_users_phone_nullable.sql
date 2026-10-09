-- ============================================================
-- 迁移脚本：users.phone 由 NOT NULL 改为可空
-- ============================================================
-- 背景：
--   自助挂号机（REGMACHINE 角色）是部署在门诊大厅的终端设备，
--   本身没有联系电话，注册该角色账号时手机号留空是正常需求。
--   原设计 users.phone 为 VARCHAR(20) NOT NULL UNIQUE，
--   留空会触发 MySQL 1048 (23000) Column 'phone' cannot be null。
--
-- 改法：
--   仅把 users.phone 放开为 NULL。
--   UNIQUE 索引保留 —— MySQL 的 UNIQUE 允许多行为 NULL，
--   因此可以有任意多个「不填手机号」的账号共存，互不冲突。
--   patients.phone 保持 NOT NULL（患者建档必须能联系到人）。
--
-- 幂等：可重复执行。
-- ============================================================

USE hospital_outpatient;

ALTER TABLE users
  MODIFY COLUMN phone VARCHAR(20) NULL COMMENT '联系电话（自助终端等岗位可留空）';

-- 校验
SELECT
  COLUMN_NAME,
  COLUMN_TYPE,
  IS_NULLABLE,
  COLUMN_COMMENT
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE()
  AND TABLE_NAME = 'users'
  AND COLUMN_NAME = 'phone';
