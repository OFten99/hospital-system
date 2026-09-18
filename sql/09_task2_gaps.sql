-- 医院门诊管理系统：任务书缺口补全（09）
-- 覆盖：药房角色与发药、检验项目计费收费、诊断结果、用户管理支撑
-- 可单独执行（增量），不破坏既有业务数据；所有结构变更均为幂等写法

USE hospital_outpatient;

-- ============================================================
-- 一、结构扩展（幂等：动态判断列是否存在）
-- ============================================================

SET @has_col = (SELECT COUNT(*) FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = 'hospital_outpatient' AND TABLE_NAME = 'lab_items' AND COLUMN_NAME = 'price');
SET @ddl = IF(@has_col = 0,
  'ALTER TABLE lab_items ADD COLUMN price DECIMAL(10,2) NOT NULL DEFAULT 0.00 COMMENT ''检验项目单价（元）'' AFTER reference_range',
  'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @has_col = (SELECT COUNT(*) FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = 'hospital_outpatient' AND TABLE_NAME = 'lab_tests' AND COLUMN_NAME = 'total_fee');
SET @ddl = IF(@has_col = 0,
  'ALTER TABLE lab_tests ADD COLUMN total_fee DECIMAL(10,2) NOT NULL DEFAULT 0.00 COMMENT ''检验费用合计（元）'' AFTER sample_type',
  'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @has_col = (SELECT COUNT(*) FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = 'hospital_outpatient' AND TABLE_NAME = 'patients' AND COLUMN_NAME = 'diagnosis');
SET @ddl = IF(@has_col = 0,
  'ALTER TABLE patients ADD COLUMN diagnosis VARCHAR(500) NULL COMMENT ''诊断结果'' AFTER medical_history',
  'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @has_col = (SELECT COUNT(*) FROM information_schema.COLUMNS
                WHERE TABLE_SCHEMA = 'hospital_outpatient' AND TABLE_NAME = 'payments' AND COLUMN_NAME = 'lab_test_id');
SET @ddl = IF(@has_col = 0,
  'ALTER TABLE payments ADD COLUMN lab_test_id INT NULL COMMENT ''关联检验单（检查费）'' AFTER prescription_id',
  'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- 处方状态机加入"已发药"（同一定义重复执行安全）
ALTER TABLE prescriptions
  MODIFY COLUMN prescription_status ENUM('待收费','已收费','已发药','已退费','已作废')
  NOT NULL DEFAULT '待收费' COMMENT '处方状态';

-- ============================================================
-- 二、索引与外键（幂等）
-- ============================================================
SET @has_idx = (SELECT COUNT(*) FROM information_schema.STATISTICS
                WHERE TABLE_SCHEMA = 'hospital_outpatient' AND TABLE_NAME = 'payments' AND INDEX_NAME = 'idx_pay_lab_test');
SET @ddl = IF(@has_idx = 0,
  'CREATE INDEX idx_pay_lab_test ON payments(lab_test_id)',
  'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

SET @has_fk = (SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
               WHERE TABLE_SCHEMA = 'hospital_outpatient' AND TABLE_NAME = 'payments' AND CONSTRAINT_NAME = 'fk_pay_lab_test');
SET @ddl = IF(@has_fk = 0,
  'ALTER TABLE payments ADD CONSTRAINT fk_pay_lab_test FOREIGN KEY (lab_test_id) REFERENCES lab_tests(test_id)',
  'SELECT 1');
PREPARE stmt FROM @ddl; EXECUTE stmt; DEALLOCATE PREPARE stmt;

-- ============================================================
-- 三、药房角色与账号（先删引用方 users，再删 roles，保证幂等）
-- ============================================================
DELETE FROM users WHERE username = 'pharm01';
DELETE FROM roles WHERE role_code = 'PHARMACIST';
INSERT INTO roles (role_code, role_name, description)
VALUES ('PHARMACIST', '药房人员', '处方发药、药品库存管理');
SET @pharm_role_id = LAST_INSERT_ID();

CALL sp_add_user('P0001', '药房人员小王', '男', NULL, @pharm_role_id, '13700000020', 'pharm01', SHA2('123456', 256));

-- ============================================================
-- 四、检验项目单价
-- ============================================================
UPDATE lab_items SET price = 15.00 WHERE item_code = 'WBC';
UPDATE lab_items SET price = 10.00 WHERE item_code IN ('RBC', 'HGB', 'PLT');
UPDATE lab_items SET price = 8.00  WHERE item_code IN ('URPRO', 'URGLU', 'URBLD');
UPDATE lab_items SET price = 12.00 WHERE item_code IN ('ALT', 'AST', 'BUN');
UPDATE lab_items SET price = 15.00 WHERE item_code IN ('TBIL', 'CREA');
UPDATE lab_items SET price = 8.00  WHERE item_code IN ('FBG');
UPDATE lab_items SET price = 10.00 WHERE item_code IN ('TC', 'TG');
UPDATE lab_items SET price = 25.00 WHERE item_code = 'CRP';

-- ============================================================
-- 五、存储过程
-- ============================================================
DROP PROCEDURE IF EXISTS sp_add_lab_test;
DROP PROCEDURE IF EXISTS sp_dispense;
DROP PROCEDURE IF EXISTS sp_charge_lab_test;

DELIMITER $$

-- 1. 开具检验申请（重写：计算检验费用合计）
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
  DECLARE v_fee DECIMAL(10,2);
  DECLARE EXIT HANDLER FOR SQLEXCEPTION
  BEGIN
    ROLLBACK;
    RESIGNAL;
  END;

  START TRANSACTION;

  IF NOT EXISTS (SELECT 1 FROM patients WHERE patient_id = p_patient_id) THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '患者不存在';
  END IF;

  SELECT COALESCE(SUM(price), 0) INTO v_fee
  FROM lab_items WHERE FIND_IN_SET(item_id, p_item_ids) > 0;

  IF v_fee = 0 THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '请至少选择一个有效的检验项目';
  END IF;

  INSERT INTO lab_tests (test_no, patient_id, doctor_id, department_id,
                         registration_id, sample_type, total_fee, test_status, created_by)
  VALUES (p_test_no, p_patient_id, p_doctor_id, p_department_id,
          p_registration_id, p_sample_type, v_fee, '待检验', p_operator_id);
  SET v_test_id = LAST_INSERT_ID();

  INSERT INTO lab_test_results (test_id, item_id)
  SELECT v_test_id, item_id FROM lab_items
  WHERE FIND_IN_SET(item_id, p_item_ids) > 0;

  INSERT INTO operation_logs (user_id, business_type, business_id, operation_content)
  VALUES (p_operator_id, '检验申请', v_test_id, CONCAT('开具检验单：', p_test_no, '，费用：', v_fee));

  COMMIT;
END$$

-- 2. 药房发药：校验已收费与库存，处方置为"已发药"
CREATE PROCEDURE sp_dispense(
  IN p_prescription_id INT,
  IN p_operator_id INT
)
BEGIN
  DECLARE v_status VARCHAR(10);
  DECLARE v_medicine_id INT;
  DECLARE v_quantity INT;
  DECLARE v_done INT DEFAULT 0;
  DECLARE v_shortage VARCHAR(200) DEFAULT '';
  DECLARE cur_items CURSOR FOR
    SELECT medicine_id, quantity FROM prescription_items WHERE prescription_id = p_prescription_id;
  DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_done = 1;
  DECLARE EXIT HANDLER FOR SQLEXCEPTION
  BEGIN
    ROLLBACK;
    RESIGNAL;
  END;

  START TRANSACTION;

  SELECT prescription_status INTO v_status
  FROM prescriptions WHERE prescription_id = p_prescription_id FOR UPDATE;

  IF v_status IS NULL THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '处方不存在';
  END IF;
  IF v_status <> '已收费' THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '仅已收费处方可以发药';
  END IF;

  -- 发药复核库存（库存已于收费时扣减，这里校验一致性）
  OPEN cur_items;
  check_loop: LOOP
    FETCH cur_items INTO v_medicine_id, v_quantity;
    IF v_done = 1 THEN
      LEAVE check_loop;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM medicines
                   WHERE medicine_id = v_medicine_id AND stock_quantity >= v_quantity) THEN
      SET v_shortage = CONCAT(v_shortage, v_medicine_id, ';');
    END IF;
  END LOOP;
  CLOSE cur_items;

  IF v_shortage <> '' THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '药品库存不足，请先补充库存后再发药';
  END IF;

  UPDATE prescriptions SET prescription_status = '已发药'
  WHERE prescription_id = p_prescription_id;

  INSERT INTO operation_logs (user_id, business_type, business_id, operation_content)
  VALUES (p_operator_id, '发药', p_prescription_id, CONCAT('处方发药：', p_prescription_id));

  COMMIT;
END$$

-- 3. 检验/检查收费
CREATE PROCEDURE sp_charge_lab_test(
  IN p_payment_no VARCHAR(40),
  IN p_test_id INT,
  IN p_pay_method VARCHAR(10),
  IN p_operator_id INT
)
BEGIN
  DECLARE v_patient_id INT;
  DECLARE v_fee DECIMAL(10,2);
  DECLARE v_status VARCHAR(10);
  DECLARE v_already INT;
  DECLARE EXIT HANDLER FOR SQLEXCEPTION
  BEGIN
    ROLLBACK;
    RESIGNAL;
  END;

  START TRANSACTION;

  SELECT patient_id, total_fee, test_status INTO v_patient_id, v_fee, v_status
  FROM lab_tests WHERE test_id = p_test_id FOR UPDATE;

  IF v_patient_id IS NULL THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '检验单不存在';
  END IF;
  IF v_status = '已作废' THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '已作废检验单不能收费';
  END IF;

  SELECT COUNT(*) INTO v_already
  FROM payments WHERE lab_test_id = p_test_id AND pay_status = '已收费';
  IF v_already > 0 THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '该检验单已收费，不能重复收费';
  END IF;

  INSERT INTO payments (payment_no, patient_id, registration_id, lab_test_id,
                        payment_type, pay_amount, pay_method, pay_status, operator_id)
  VALUES (p_payment_no, v_patient_id, NULL, p_test_id,
          '检查费', v_fee, p_pay_method, '已收费', p_operator_id);

  INSERT INTO operation_logs (user_id, business_type, business_id, operation_content)
  VALUES (p_operator_id, '检验收费', p_test_id, CONCAT('检验单收费：', p_payment_no, '，金额：', v_fee));

  COMMIT;
END$$

DELIMITER ;

-- ============================================================
-- 六、视图更新（检验单列表加费用与收费状态）
-- ============================================================
DROP VIEW IF EXISTS v_lab_test_list;
CREATE VIEW v_lab_test_list AS
SELECT
  t.test_id, t.test_no, t.sample_type, t.total_fee, t.test_status, t.ordered_at, t.completed_at,
  p.patient_id, p.patient_name,
  u.user_name AS doctor_name,
  d.department_id, d.department_name,
  COUNT(r.result_id) AS item_count,
  SUM(CASE WHEN r.result_value IS NOT NULL AND r.result_value <> '' THEN 1 ELSE 0 END) AS result_count,
  CASE WHEN EXISTS (SELECT 1 FROM payments py
                    WHERE py.lab_test_id = t.test_id AND py.pay_status = '已收费')
       THEN '已收费' ELSE '未收费' END AS pay_status
FROM lab_tests t
JOIN patients p   ON t.patient_id = p.patient_id
JOIN doctors doc  ON t.doctor_id = doc.doctor_id
JOIN users u      ON doc.user_id = u.user_id
JOIN departments d ON t.department_id = d.department_id
LEFT JOIN lab_test_results r ON r.test_id = t.test_id
GROUP BY t.test_id, t.test_no, t.sample_type, t.total_fee, t.test_status, t.ordered_at, t.completed_at,
         p.patient_id, p.patient_name, u.user_name, d.department_id, d.department_name;
