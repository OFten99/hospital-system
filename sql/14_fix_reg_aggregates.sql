-- 医院门诊管理系统：修正「当日挂号统计视图」把退号计入合计的问题
--
-- 背景（2026-10-09）：
--   v_today_registrations 原先把「退号」的挂号费也算进 total_fee，
--   并且 registration_count 也把退号记录计入。用户在首页看板的
--   「今日挂号 / 挂号费合计」与「报表 - 挂号统计」里看到的总计
--   包含了已经退号的金额，与「今日收费金额」对不上。
--
--   「退号」在业务上意味着这笔挂号费已经退还（sp_cancel_registration
--   会把对应 payments 置为「已退费」），因此统计口径应为：
--     * registration_count  只统计有效挂号（不含已退号）
--     * normal_count/expert_count 同样不含已退号
--     * total_fee 只累加有效挂号的挂号费
--     * cancelled_count 仍然单独给出退号笔数，便于对照
--
-- 说明：这里沿用本项目既有约定 —— 用 DROP ... IF EXISTS + CREATE 覆盖升级，
--       （不用 CREATE OR REPLACE），与 09/10/11/13 脚本保持一致。

USE hospital_outpatient;

DROP VIEW IF EXISTS v_today_registrations;

CREATE VIEW v_today_registrations AS
SELECT
  d.department_name,
  -- 有效挂号数（不含已退号）
  SUM(CASE WHEN r.visit_status <> '已退号' THEN 1 ELSE 0 END) AS registration_count,
  SUM(CASE WHEN r.visit_status <> '已退号' AND r.reg_type = '普通号' THEN 1 ELSE 0 END) AS normal_count,
  SUM(CASE WHEN r.visit_status <> '已退号' AND r.reg_type = '专家号' THEN 1 ELSE 0 END) AS expert_count,
  -- 挂号费合计：只累加有效挂号，退号的挂号费不计入
  COALESCE(SUM(CASE WHEN r.visit_status <> '已退号' THEN r.reg_fee ELSE 0 END), 0) AS total_fee,
  -- 退号笔数单独统计，供页面上对照展示
  SUM(CASE WHEN r.visit_status = '已退号' THEN 1 ELSE 0 END) AS cancelled_count
FROM registrations r
JOIN departments d ON r.department_id = d.department_id
WHERE r.reg_date = CURDATE()
GROUP BY d.department_id, d.department_name;
