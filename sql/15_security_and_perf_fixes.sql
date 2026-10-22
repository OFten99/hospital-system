-- 15_security_and_perf_fixes.sql
-- 代码审查后的数据库层修复（可重复执行，幂等）
--
-- 内容：
--   1. patients.phone 放开可空 —— 线上自助建档不填手机号时不再撞唯一索引；
--   2. 补两个索引 —— 收费日报 / 检验统计改用时间区间查询后可命中索引；
--   3. 重建 sp_register        —— 排队号按「当天已发出的最大号 + 1」取，退号不重号；
--   4. 重建 sp_charge          —— 库存不足时报错回滚，不再静默扣成 0；
--   5. 重建 sp_record_lab_result —— 已完成的检验单不允许再改结果。
--
-- ⚠️ 已存在的库执行本脚本即可生效；从零建库时 init_database.py 会自动执行它
--    （02/04/08 号脚本里也已同步同样的定义）。

USE hospital_outpatient;

-- ============================================================
-- 1. 患者手机号可空
-- ============================================================
ALTER TABLE patients
  MODIFY COLUMN phone VARCHAR(20) NULL COMMENT '联系电话（线上自助建档可空）';

-- ============================================================
-- 2. 索引（先查 information_schema，避免重复执行报 1061）
-- ============================================================
-- 看板/收费日报：WHERE pay_time >= ? AND pay_time < ? AND pay_status = '已收费'
-- 现有 idx_payment_status_time 是 (pay_status, pay_time)，当前导列没有等值条件时
-- 用不上；单独给 pay_time 建一个，范围查询可以直接走索引。
SET @ddl = (SELECT IF(COUNT(*) = 0,
    'CREATE INDEX idx_payment_pay_time ON payments(pay_time)',
    'DO 0')
  FROM information_schema.statistics
  WHERE table_schema = DATABASE() AND table_name = 'payments'
    AND index_name = 'idx_payment_pay_time');
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 检验科「今日申请」统计：WHERE ordered_at >= CURDATE() AND ordered_at < ...
SET @ddl = (SELECT IF(COUNT(*) = 0,
    'CREATE INDEX idx_labtest_ordered_at ON lab_tests(ordered_at)',
    'DO 0')
  FROM information_schema.statistics
  WHERE table_schema = DATABASE() AND table_name = 'lab_tests'
    AND index_name = 'idx_labtest_ordered_at');
PREPARE stmt FROM @ddl;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- ============================================================
-- 3~5. 重建三个存储过程
-- ============================================================
DROP PROCEDURE IF EXISTS sp_register;
DROP PROCEDURE IF EXISTS sp_charge;
DROP PROCEDURE IF EXISTS sp_record_lab_result;

DELIMITER $$

-- 挂号：排队号改为「当天该医生已发出的最大号 + 1」
CREATE PROCEDURE sp_register(
  IN p_reg_no VARCHAR(40),
  IN p_patient_id INT,
  IN p_doctor_id INT,
  IN p_schedule_id INT,
  IN p_reg_type VARCHAR(10),
  IN p_operator_id INT,
  IN p_pay_method VARCHAR(10)
)
BEGIN
  DECLARE v_department_id INT;
  DECLARE v_fee DECIMAL(10,2);
  DECLARE v_registered INT;
  DECLARE v_max INT;
  DECLARE v_status VARCHAR(10);
  DECLARE v_doctor_id INT;
  DECLARE v_queue INT;
  DECLARE v_registration_id INT;
  DECLARE v_payment_no VARCHAR(40);
  DECLARE EXIT HANDLER FOR SQLEXCEPTION
  BEGIN
    ROLLBACK;
    RESIGNAL;
  END;

  START TRANSACTION;

  SELECT max_registrations, registered_count, schedule_status, doctor_id
  INTO v_max, v_registered, v_status, v_doctor_id
  FROM doctor_schedules
  WHERE schedule_id = p_schedule_id
  FOR UPDATE;

  IF v_max IS NULL THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '排班不存在';
  END IF;
  IF v_status = '停诊' THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '该排班已停诊，不能挂号';
  END IF;
  IF v_registered >= v_max THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '号源已满，无法挂号';
  END IF;

  SELECT consultation_fee INTO v_fee FROM doctors WHERE doctor_id = p_doctor_id;
  SELECT department_id INTO v_department_id
  FROM users u JOIN doctors doc ON u.user_id = doc.user_id
  WHERE doc.doctor_id = p_doctor_id;

  -- 退号会让 registered_count 减 1，若仍用 registered_count + 1 取号，
  -- 下一位患者就会拿到已经叫过的号码。改为取当天已发出的最大排队号 + 1。
  SELECT COALESCE(MAX(queue_no), 0) + 1 INTO v_queue
  FROM registrations
  WHERE doctor_id = p_doctor_id AND reg_date = CURDATE();

  INSERT INTO registrations (
    reg_no, patient_id, doctor_id, department_id, reg_date,
    reg_type, reg_fee, queue_no, visit_status, operator_id
  ) VALUES (
    p_reg_no, p_patient_id, p_doctor_id, v_department_id, CURDATE(),
    p_reg_type, v_fee, v_queue, '待就诊', p_operator_id
  );
  SET v_registration_id = LAST_INSERT_ID();

  UPDATE doctor_schedules
  SET registered_count = registered_count + 1
  WHERE schedule_id = p_schedule_id;

  SET v_payment_no = CONCAT('PAY', DATE_FORMAT(NOW(), '%Y%m%d%H%i%s'), LPAD(v_registration_id, 4, '0'));
  INSERT INTO payments (
    payment_no, patient_id, registration_id, payment_type,
    pay_amount, pay_method, pay_status, operator_id
  ) VALUES (
    v_payment_no, p_patient_id, v_registration_id, '挂号费',
    v_fee, p_pay_method, '已收费', p_operator_id
  );

  COMMIT;
END$$

-- 收费：库存不足报错回滚，不再静默扣成 0
CREATE PROCEDURE sp_charge(
  IN p_prescription_id INT,
  IN p_operator_id INT,
  IN p_payment_no VARCHAR(40),
  IN p_pay_method VARCHAR(10)
)
BEGIN
  DECLARE v_patient_id INT;
  DECLARE v_status VARCHAR(10);
  DECLARE v_total DECIMAL(10,2);
  DECLARE v_done INT DEFAULT 0;
  DECLARE v_medicine_id INT;
  DECLARE v_quantity INT;
  DECLARE cur_items CURSOR FOR
    SELECT medicine_id, quantity FROM prescription_items WHERE prescription_id = p_prescription_id;
  DECLARE CONTINUE HANDLER FOR NOT FOUND SET v_done = 1;
  DECLARE EXIT HANDLER FOR SQLEXCEPTION
  BEGIN
    ROLLBACK;
    RESIGNAL;
  END;

  START TRANSACTION;

  SELECT patient_id, prescription_status, total_amount
  INTO v_patient_id, v_status, v_total
  FROM prescriptions
  WHERE prescription_id = p_prescription_id
  FOR UPDATE;

  IF v_patient_id IS NULL THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '处方不存在';
  END IF;
  IF v_status <> '待收费' THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '仅待收费状态的处方可以收费';
  END IF;

  INSERT INTO payments (
    payment_no, patient_id, prescription_id, payment_type,
    pay_amount, pay_method, pay_status, operator_id
  ) VALUES (
    p_payment_no, v_patient_id, p_prescription_id, '药品费',
    v_total, p_pay_method, '已收费', p_operator_id
  );

  UPDATE prescriptions
  SET prescription_status = '已收费'
  WHERE prescription_id = p_prescription_id;

  OPEN cur_items;
  stock_loop: LOOP
    FETCH cur_items INTO v_medicine_id, v_quantity;
    IF v_done = 1 THEN
      LEAVE stock_loop;
    END IF;
    -- 条件扣减：库存不够时 ROW_COUNT() = 0，立即报错让整笔收费回滚。
    -- 原实现用 GREATEST(..., 0) 会把库存悄悄抹成 0，钱收了药却发不出。
    UPDATE medicines
    SET stock_quantity = stock_quantity - v_quantity
    WHERE medicine_id = v_medicine_id
      AND stock_quantity >= v_quantity;
    IF ROW_COUNT() = 0 THEN
      SIGNAL SQLSTATE '45000'
        SET MESSAGE_TEXT = '药品库存不足，无法收费，请调整处方或补充库存';
    END IF;
  END LOOP;
  CLOSE cur_items;

  COMMIT;
END$$

-- 检验结果录入：已完成的检验单不允许再改
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
  DECLARE v_paid INT;
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
  IF v_status = '已完成' THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '检验单已完成，结果不能再修改';
  END IF;

  SELECT COUNT(*) INTO v_paid
  FROM payments
  WHERE lab_test_id = p_test_id AND pay_status = '已收费';
  IF v_paid = 0 THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '该检验单尚未收费，请先到收费窗口缴费后再录入结果';
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

DELIMITER ;
