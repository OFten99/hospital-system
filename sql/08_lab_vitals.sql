-- 医院门诊管理系统：检验科模块 + 患者体征记录
-- 建议执行顺序：01 -> ... -> 07 -> 08（本脚本可单独执行，不破坏既有业务数据）
-- 覆盖：检验项目字典、检验申请单、检验结果明细、体征记录，以及对应视图/存储过程/初始数据

USE hospital_outpatient;

-- ============================================================
-- 一、建表（先删子表再删父表）
-- ============================================================
DROP TABLE IF EXISTS lab_test_results;
DROP TABLE IF EXISTS lab_tests;
DROP TABLE IF EXISTS vital_signs;
DROP TABLE IF EXISTS lab_items;

-- 1. 检验项目字典
CREATE TABLE lab_items (
  item_id         INT AUTO_INCREMENT PRIMARY KEY,
  item_code       VARCHAR(20)  NOT NULL UNIQUE COMMENT '检验项目编码',
  item_name       VARCHAR(50)  NOT NULL COMMENT '检验项目名称',
  item_category   VARCHAR(20)  NOT NULL COMMENT '项目分类：血常规/尿常规/肝功能/肾功能/血糖血脂/炎症标志物',
  unit            VARCHAR(20)  NULL COMMENT '单位',
  reference_range VARCHAR(50)  NULL COMMENT '参考范围',
  status          VARCHAR(10)  NOT NULL DEFAULT '启用' COMMENT '启用/停用'
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='检验项目字典';

-- 2. 患者体征记录表（医生录入）
CREATE TABLE vital_signs (
  vital_id          INT AUTO_INCREMENT PRIMARY KEY,
  patient_id        INT          NOT NULL COMMENT '患者',
  registration_id   INT          NULL COMMENT '关联挂号（可为空）',
  operator_id       INT          NOT NULL COMMENT '录入医生（users.user_id）',
  temperature       DECIMAL(4,1) NULL COMMENT '体温（℃）',
  systolic_pressure INT          NULL COMMENT '收缩压（mmHg）',
  diastolic_pressure INT         NULL COMMENT '舒张压（mmHg）',
  heart_rate        INT          NULL COMMENT '心率（次/分）',
  respiration_rate  INT          NULL COMMENT '呼吸（次/分）',
  weight            DECIMAL(5,2) NULL COMMENT '体重（kg）',
  height            DECIMAL(5,2) NULL COMMENT '身高（cm）',
  record_time       DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '测量时间',
  note              VARCHAR(200) NULL COMMENT '备注',
  created_at        DATETIME     NOT NULL DEFAULT CURRENT_TIMESTAMP,
  CONSTRAINT fk_vital_patient  FOREIGN KEY (patient_id)      REFERENCES patients(patient_id),
  CONSTRAINT fk_vital_reg      FOREIGN KEY (registration_id) REFERENCES registrations(registration_id),
  CONSTRAINT fk_vital_operator FOREIGN KEY (operator_id)     REFERENCES users(user_id),
  INDEX idx_vital_patient_time (patient_id, record_time)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='患者体征记录';

-- 3. 检验申请单
CREATE TABLE lab_tests (
  test_id        INT AUTO_INCREMENT PRIMARY KEY,
  test_no        VARCHAR(30) NOT NULL UNIQUE COMMENT '检验单号',
  patient_id     INT         NOT NULL COMMENT '患者',
  doctor_id      INT         NOT NULL COMMENT '开单医生（doctors.doctor_id）',
  department_id  INT         NOT NULL COMMENT '开单科室',
  registration_id INT        NULL COMMENT '关联挂号（可为空）',
  sample_type    VARCHAR(20) NOT NULL DEFAULT '血液' COMMENT '标本类型：血液/尿液/粪便/其他',
  test_status    VARCHAR(10) NOT NULL DEFAULT '待检验' COMMENT '待检验/检验中/已完成/已作废',
  ordered_at     DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '开单时间',
  completed_at   DATETIME    NULL COMMENT '完成时间',
  created_by     INT         NULL COMMENT '开单操作人（users.user_id）',
  CONSTRAINT fk_labtest_patient FOREIGN KEY (patient_id)      REFERENCES patients(patient_id),
  CONSTRAINT fk_labtest_doctor  FOREIGN KEY (doctor_id)       REFERENCES doctors(doctor_id),
  CONSTRAINT fk_labtest_dept    FOREIGN KEY (department_id)   REFERENCES departments(department_id),
  CONSTRAINT fk_labtest_reg     FOREIGN KEY (registration_id) REFERENCES registrations(registration_id),
  INDEX idx_labtest_patient (patient_id),
  INDEX idx_labtest_status (test_status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='检验申请单';

-- 4. 检验结果明细
CREATE TABLE lab_test_results (
  result_id    INT AUTO_INCREMENT PRIMARY KEY,
  test_id      INT         NOT NULL COMMENT '检验单',
  item_id      INT         NOT NULL COMMENT '检验项目',
  result_value VARCHAR(50) NULL COMMENT '检验结果值（NULL 表示未出结果）',
  result_flag  VARCHAR(10) NOT NULL DEFAULT '正常' COMMENT '正常/偏高/偏低/异常',
  result_note  VARCHAR(200) NULL COMMENT '备注',
  operator_id  INT         NULL COMMENT '录入人（users.user_id）',
  created_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at   DATETIME    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  UNIQUE KEY uk_test_item (test_id, item_id),
  CONSTRAINT fk_labresult_test FOREIGN KEY (test_id) REFERENCES lab_tests(test_id),
  CONSTRAINT fk_labresult_item FOREIGN KEY (item_id) REFERENCES lab_items(item_id),
  CONSTRAINT fk_labresult_op   FOREIGN KEY (operator_id) REFERENCES users(user_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='检验结果明细';

-- ============================================================
-- 二、视图
-- ============================================================
DROP VIEW IF EXISTS v_lab_test_list;
DROP VIEW IF EXISTS v_lab_result_detail;
DROP VIEW IF EXISTS v_lab_patient_summary;
DROP VIEW IF EXISTS v_vital_history;

-- 1. 检验单列表视图
CREATE VIEW v_lab_test_list AS
SELECT
  t.test_id, t.test_no, t.sample_type, t.test_status, t.ordered_at, t.completed_at,
  p.patient_id, p.patient_name,
  u.user_name AS doctor_name,
  d.department_id, d.department_name,
  COUNT(r.result_id) AS item_count,
  SUM(CASE WHEN r.result_value IS NOT NULL AND r.result_value <> '' THEN 1 ELSE 0 END) AS result_count
FROM lab_tests t
JOIN patients p   ON t.patient_id = p.patient_id
JOIN doctors doc  ON t.doctor_id = doc.doctor_id
JOIN users u      ON doc.user_id = u.user_id
JOIN departments d ON t.department_id = d.department_id
LEFT JOIN lab_test_results r ON r.test_id = t.test_id
GROUP BY t.test_id, t.test_no, t.sample_type, t.test_status, t.ordered_at, t.completed_at,
         p.patient_id, p.patient_name, u.user_name, d.department_id, d.department_name;

-- 2. 检验结果明细视图
CREATE VIEW v_lab_result_detail AS
SELECT
  t.test_id, t.test_no, t.test_status,
  p.patient_id, p.patient_name,
  li.item_id, li.item_name, li.item_category, li.unit, li.reference_range,
  r.result_id, r.result_value, r.result_flag, r.result_note, r.updated_at AS result_time,
  u2.user_name AS operator_name
FROM lab_test_results r
JOIN lab_tests t  ON r.test_id = t.test_id
JOIN lab_items li ON r.item_id = li.item_id
JOIN patients p   ON t.patient_id = p.patient_id
LEFT JOIN users u2 ON r.operator_id = u2.user_id;

-- 3. 按患者汇总视图（检验科数据汇总）
CREATE VIEW v_lab_patient_summary AS
SELECT
  p.patient_id, p.patient_name,
  COUNT(t.test_id) AS test_count,
  SUM(CASE WHEN t.test_status = '已完成' THEN 1 ELSE 0 END) AS done_count,
  SUM(CASE WHEN r.result_flag IN ('偏高','偏低','异常') THEN 1 ELSE 0 END) AS abnormal_count,
  MAX(t.test_no) AS latest_test_no,
  (SELECT t2.test_status FROM lab_tests t2
    WHERE t2.patient_id = p.patient_id
    ORDER BY t2.ordered_at DESC, t2.test_id DESC LIMIT 1) AS latest_status
FROM patients p
LEFT JOIN lab_tests t ON t.patient_id = p.patient_id
LEFT JOIN lab_test_results r ON r.test_id = t.test_id
GROUP BY p.patient_id, p.patient_name;

-- 4. 体征历史视图
CREATE VIEW v_vital_history AS
SELECT
  v.vital_id, v.patient_id, p.patient_name,
  u.user_name AS operator_name,
  v.temperature, v.systolic_pressure, v.diastolic_pressure, v.heart_rate,
  v.respiration_rate, v.weight, v.height, v.record_time, v.note
FROM vital_signs v
JOIN patients p ON v.patient_id = p.patient_id
JOIN users u    ON v.operator_id = u.user_id;

-- ============================================================
-- 三、存储过程
-- ============================================================
DROP PROCEDURE IF EXISTS sp_add_vital_sign;
DROP PROCEDURE IF EXISTS sp_add_lab_test;
DROP PROCEDURE IF EXISTS sp_record_lab_result;
DROP PROCEDURE IF EXISTS sp_cancel_lab_test;

DELIMITER $$

-- 1. 录入患者体征（医生）
CREATE PROCEDURE sp_add_vital_sign(
  IN p_patient_id INT,
  IN p_registration_id INT,
  IN p_operator_id INT,
  IN p_temperature DECIMAL(4,1),
  IN p_systolic INT,
  IN p_diastolic INT,
  IN p_heart_rate INT,
  IN p_respiration INT,
  IN p_weight DECIMAL(5,2),
  IN p_height DECIMAL(5,2),
  IN p_note VARCHAR(200)
)
BEGIN
  DECLARE EXIT HANDLER FOR SQLEXCEPTION
  BEGIN
    ROLLBACK;
    RESIGNAL;
  END;

  START TRANSACTION;

  IF NOT EXISTS (SELECT 1 FROM patients WHERE patient_id = p_patient_id) THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '患者不存在';
  END IF;

  IF p_temperature IS NOT NULL AND (p_temperature < 30 OR p_temperature > 45) THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '体温超出合理范围（30~45℃）';
  END IF;
  IF p_systolic IS NOT NULL AND (p_systolic < 40 OR p_systolic > 300) THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '收缩压超出合理范围（40~300 mmHg）';
  END IF;
  IF p_diastolic IS NOT NULL AND (p_diastolic < 30 OR p_diastolic > 200) THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '舒张压超出合理范围（30~200 mmHg）';
  END IF;
  IF p_heart_rate IS NOT NULL AND (p_heart_rate < 20 OR p_heart_rate > 250) THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '心率超出合理范围（20~250 次/分）';
  END IF;
  IF p_respiration IS NOT NULL AND (p_respiration < 5 OR p_respiration > 80) THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '呼吸频率超出合理范围（5~80 次/分）';
  END IF;
  IF p_weight IS NOT NULL AND (p_weight < 1 OR p_weight > 400) THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '体重超出合理范围（1~400 kg）';
  END IF;

  INSERT INTO vital_signs (patient_id, registration_id, operator_id, temperature,
                           systolic_pressure, diastolic_pressure, heart_rate,
                           respiration_rate, weight, height, note)
  VALUES (p_patient_id, p_registration_id, p_operator_id, p_temperature,
          p_systolic, p_diastolic, p_heart_rate,
          p_respiration, p_weight, p_height, p_note);

  INSERT INTO operation_logs (user_id, business_type, business_id, operation_content)
  VALUES (p_operator_id, '体征记录', p_patient_id,
          CONCAT('录入患者 ', p_patient_id, ' 体征记录'));

  COMMIT;
END$$

-- 2. 开具检验申请（医生/管理员）
CREATE PROCEDURE sp_add_lab_test(
  IN p_test_no VARCHAR(30),
  IN p_patient_id INT,
  IN p_doctor_id INT,
  IN p_department_id INT,
  IN p_registration_id INT,
  IN p_sample_type VARCHAR(20),
  IN p_item_ids VARCHAR(255),
  IN p_operator_id INT
)
BEGIN
  DECLARE v_test_id INT;
  DECLARE EXIT HANDLER FOR SQLEXCEPTION
  BEGIN
    ROLLBACK;
    RESIGNAL;
  END;

  START TRANSACTION;

  IF NOT EXISTS (SELECT 1 FROM patients WHERE patient_id = p_patient_id) THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '患者不存在';
  END IF;

  INSERT INTO lab_tests (test_no, patient_id, doctor_id, department_id,
                         registration_id, sample_type, test_status, created_by)
  VALUES (p_test_no, p_patient_id, p_doctor_id, p_department_id,
          p_registration_id, p_sample_type, '待检验', p_operator_id);
  SET v_test_id = LAST_INSERT_ID();

  INSERT INTO lab_test_results (test_id, item_id)
  SELECT v_test_id, item_id FROM lab_items
  WHERE FIND_IN_SET(item_id, p_item_ids) > 0;

  IF ROW_COUNT() = 0 THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '请至少选择一个检验项目';
  END IF;

  INSERT INTO operation_logs (user_id, business_type, business_id, operation_content)
  VALUES (p_operator_id, '检验申请', v_test_id, CONCAT('开具检验单：', p_test_no));

  COMMIT;
END$$

-- 3. 录入检验结果（检验技师），自动流转状态
CREATE PROCEDURE sp_record_lab_result(
  IN p_test_id INT,
  IN p_result_id INT,
  IN p_result_value VARCHAR(50),
  IN p_result_flag VARCHAR(10),
  IN p_result_note VARCHAR(200),
  IN p_operator_id INT
)
BEGIN
  DECLARE v_status VARCHAR(10);
  DECLARE v_pending INT;
  DECLARE EXIT HANDLER FOR SQLEXCEPTION
  BEGIN
    ROLLBACK;
    RESIGNAL;
  END;

  START TRANSACTION;

  SELECT test_status INTO v_status
  FROM lab_tests WHERE test_id = p_test_id FOR UPDATE;

  IF v_status IS NULL THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '检验单不存在';
  END IF;
  IF v_status = '已作废' THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '检验单已作废，不能录入结果';
  END IF;

  UPDATE lab_test_results
  SET result_value = p_result_value,
      result_flag  = p_result_flag,
      result_note  = p_result_note,
      operator_id  = p_operator_id
  WHERE result_id = p_result_id AND test_id = p_test_id;

  IF ROW_COUNT() = 0 THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '检验结果条目不存在或不属于该检验单';
  END IF;

  -- 状态流转：全部项目有结果 -> 已完成；部分有结果 -> 检验中
  SELECT COUNT(*) INTO v_pending
  FROM lab_test_results
  WHERE test_id = p_test_id
    AND (result_value IS NULL OR result_value = '');

  IF v_pending = 0 THEN
    UPDATE lab_tests SET test_status = '已完成', completed_at = NOW()
    WHERE test_id = p_test_id;
  ELSE
    UPDATE lab_tests SET test_status = '检验中'
    WHERE test_id = p_test_id AND test_status NOT IN ('已完成', '已作废');
  END IF;

  INSERT INTO operation_logs (user_id, business_type, business_id, operation_content)
  VALUES (p_operator_id, '检验结果', p_test_id,
          CONCAT('检验单结果录入：条目 ', p_result_id));

  COMMIT;
END$$

-- 4. 作废检验单
CREATE PROCEDURE sp_cancel_lab_test(
  IN p_test_id INT,
  IN p_operator_id INT
)
BEGIN
  DECLARE v_status VARCHAR(10);
  DECLARE EXIT HANDLER FOR SQLEXCEPTION
  BEGIN
    ROLLBACK;
    RESIGNAL;
  END;

  START TRANSACTION;

  SELECT test_status INTO v_status
  FROM lab_tests WHERE test_id = p_test_id FOR UPDATE;

  IF v_status IS NULL THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '检验单不存在';
  END IF;
  IF v_status = '已完成' THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '已完成检验不能作废';
  END IF;
  IF v_status = '已作废' THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '检验单已作废';
  END IF;

  UPDATE lab_tests SET test_status = '已作废' WHERE test_id = p_test_id;

  INSERT INTO operation_logs (user_id, business_type, business_id, operation_content)
  VALUES (p_operator_id, '检验作废', p_test_id, CONCAT('作废检验单：', p_test_id));

  COMMIT;
END$$

DELIMITER ;

-- ============================================================
-- 四、初始数据
-- ============================================================

-- 1. 检验项目字典（幂等：先清空再插入）
DELETE FROM lab_items;
INSERT INTO lab_items (item_code, item_name, item_category, unit, reference_range, status) VALUES
('WBC',  '白细胞计数', '血常规', '10^9/L',   '3.5-9.5',     '启用'),
('RBC',  '红细胞计数', '血常规', '10^12/L',  '3.8-5.8',     '启用'),
('HGB',  '血红蛋白',   '血常规', 'g/L',      '115-175',     '启用'),
('PLT',  '血小板计数', '血常规', '10^9/L',   '125-350',     '启用'),
('URPRO','尿蛋白',     '尿常规', '定性',      '阴性',        '启用'),
('URGLU','尿糖',       '尿常规', '定性',      '阴性',        '启用'),
('URBLD','尿潜血',     '尿常规', '定性',      '阴性',        '启用'),
('ALT',  '谷丙转氨酶', '肝功能', 'U/L',       '9-50',        '启用'),
('AST',  '谷草转氨酶', '肝功能', 'U/L',       '15-40',       '启用'),
('TBIL', '总胆红素',   '肝功能', 'umol/L',    '3.4-20.5',    '启用'),
('CREA', '肌酐',       '肾功能', 'umol/L',    '57-111',      '启用'),
('BUN',  '尿素氮',     '肾功能', 'mmol/L',    '3.1-8.8',     '启用'),
('FBG',  '空腹血糖',   '血糖血脂', 'mmol/L',  '3.9-6.1',     '启用'),
('TC',   '总胆固醇',   '血糖血脂', 'mmol/L',  '2.8-5.2',     '启用'),
('TG',   '甘油三酯',   '血糖血脂', 'mmol/L',  '0.4-1.7',     '启用'),
('CRP',  'C反应蛋白',  '炎症标志物', 'mg/L',  '0-8',         '启用');

-- 2. 检验科科室与检验技师账号（幂等）
DELETE FROM departments WHERE department_code = 'JYK';
INSERT INTO departments (department_code, department_name, description)
VALUES ('JYK', '检验科', '临床检验、化验与结果报告');
SET @jyc_id = LAST_INSERT_ID();

DELETE FROM roles WHERE role_code = 'LAB_TECH';
INSERT INTO roles (role_code, role_name, description)
VALUES ('LAB_TECH', '检验技师', '检验申请处理、检验结果录入与汇总');
SET @lab_role_id = LAST_INSERT_ID();

DELETE FROM users WHERE username = 'lab01';
CALL sp_add_user('L0001', '检验技师小周', '女', @jyc_id, @lab_role_id, '13700000019', 'lab01', SHA2('123456', 256));
