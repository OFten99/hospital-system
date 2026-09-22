-- 医院门诊管理系统：预约挂号功能增量（第 10 号脚本）
-- 覆盖：预约挂号表、创建预约 / 取消预约 / 预约取号存储过程、号源池联动
-- 规则：挂号与有效预约共享号源池（已挂号 + 有效预约 <= 号源上限）
--       预约生成预约流水号与挂号时限（就诊日上午 11:00 / 下午 17:00 前取号），逾期不占用号源

USE hospital_outpatient;

-- 1. 预约挂号表
CREATE TABLE IF NOT EXISTS appointments (
  appointment_id INT AUTO_INCREMENT PRIMARY KEY,
  appointment_no VARCHAR(40) NOT NULL COMMENT '预约流水号（APT+时间戳）',
  patient_id INT NOT NULL,
  schedule_id INT NOT NULL,
  reg_type VARCHAR(10) NOT NULL DEFAULT '普通号',
  appointment_date DATE NOT NULL COMMENT '预约就诊日期（与排班日期一致）',
  expire_time DATETIME NOT NULL COMMENT '挂号时限：此时间前须取号，逾期作废',
  status VARCHAR(10) NOT NULL DEFAULT '已预约' COMMENT '已预约/已取号/已取消/已过期',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  CONSTRAINT fk_appt_patient FOREIGN KEY (patient_id) REFERENCES patients(patient_id),
  CONSTRAINT fk_appt_schedule FOREIGN KEY (schedule_id) REFERENCES doctor_schedules(schedule_id),
  KEY idx_appt_patient_date (patient_id, appointment_date),
  KEY idx_appt_schedule_status (schedule_id, status)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COMMENT='预约挂号表';

-- 2. 重建医生排班视图：增加有效预约数，剩余号源 = 上限 - 已挂号 - 有效预约
DROP VIEW IF EXISTS v_doctor_schedule;
CREATE VIEW v_doctor_schedule AS
SELECT
  s.schedule_id,
  s.work_date,
  s.shift_type,
  s.clinic_room,
  s.max_registrations,
  s.registered_count,
  (SELECT COUNT(*) FROM appointments a
    WHERE a.schedule_id = s.schedule_id AND a.status = '已预约' AND a.expire_time > NOW()) AS appointment_count,
  GREATEST(s.max_registrations - s.registered_count -
    (SELECT COUNT(*) FROM appointments a
      WHERE a.schedule_id = s.schedule_id AND a.status = '已预约' AND a.expire_time > NOW()), 0) AS remain_count,
  s.schedule_status,
  doc.doctor_no,
  doc.doctor_name,
  doc.title,
  doc.specialty,
  doc.consultation_fee,
  dept.department_name
FROM doctor_schedules s
JOIN (
  SELECT d.doctor_id, d.doctor_no, d.title, d.specialty, d.consultation_fee,
         u.user_name AS doctor_name, u.department_id
  FROM doctors d
  JOIN users u ON d.user_id = u.user_id
) doc ON s.doctor_id = doc.doctor_id
JOIN departments dept ON doc.department_id = dept.department_id;

-- 3. 重载 sp_register：号源校验纳入有效预约数（取号时排除自身预约）
DROP PROCEDURE IF EXISTS sp_register;
DELIMITER $$
CREATE PROCEDURE sp_register(
  IN p_reg_no VARCHAR(40),
  IN p_patient_id INT,
  IN p_doctor_id INT,
  IN p_schedule_id INT,
  IN p_reg_type VARCHAR(10),
  IN p_operator_id INT,
  IN p_pay_method VARCHAR(10),
  IN p_appointment_id INT
)
BEGIN
  DECLARE v_department_id INT;
  DECLARE v_fee DECIMAL(10,2);
  DECLARE v_registered INT;
  DECLARE v_appt INT;
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

  SELECT COUNT(*) INTO v_appt
  FROM appointments
  WHERE schedule_id = p_schedule_id AND status = '已预约' AND expire_time > NOW()
    AND (p_appointment_id IS NULL OR appointment_id <> p_appointment_id);

  IF v_registered + v_appt >= v_max THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '号源已满，无法挂号';
  END IF;

  SELECT consultation_fee INTO v_fee FROM doctors WHERE doctor_id = p_doctor_id;
  SELECT department_id INTO v_department_id FROM users u JOIN doctors doc ON u.user_id = doc.user_id WHERE doc.doctor_id = p_doctor_id;

  SET v_queue = v_registered + 1;

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

-- 4. 创建预约：校验号源与重复预约，生成挂号时限
DROP PROCEDURE IF EXISTS sp_create_appointment;
CREATE PROCEDURE sp_create_appointment(
  IN p_appointment_no VARCHAR(40),
  IN p_patient_id INT,
  IN p_schedule_id INT,
  IN p_reg_type VARCHAR(10),
  IN p_operator_id INT
)
BEGIN
  DECLARE v_work_date DATE;
  DECLARE v_shift VARCHAR(10);
  DECLARE v_max INT;
  DECLARE v_registered INT;
  DECLARE v_status VARCHAR(10);
  DECLARE v_appt INT;
  DECLARE v_dup INT;
  DECLARE v_expire DATETIME;
  DECLARE EXIT HANDLER FOR SQLEXCEPTION
  BEGIN
    ROLLBACK;
    RESIGNAL;
  END;

  START TRANSACTION;

  SELECT work_date, shift_type, max_registrations, registered_count, schedule_status
  INTO v_work_date, v_shift, v_max, v_registered, v_status
  FROM doctor_schedules
  WHERE schedule_id = p_schedule_id
  FOR UPDATE;

  IF v_work_date IS NULL THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '排班不存在';
  END IF;
  IF v_work_date < CURDATE() THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '该排班就诊日期已过，无法预约';
  END IF;
  IF v_status = '停诊' THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '该排班已停诊，不能预约';
  END IF;

  SET v_expire = IF(v_shift = '上午', TIMESTAMP(v_work_date, '11:00:00'), TIMESTAMP(v_work_date, '17:00:00'));
  IF v_expire <= NOW() THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '该排班挂号时限已过，请直接当日挂号';
  END IF;

  SELECT COUNT(*) INTO v_dup
  FROM appointments
  WHERE patient_id = p_patient_id AND schedule_id = p_schedule_id
    AND status = '已预约' AND expire_time > NOW();
  IF v_dup > 0 THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '该患者在此排班已有有效预约，请勿重复预约';
  END IF;

  SELECT COUNT(*) INTO v_appt
  FROM appointments
  WHERE schedule_id = p_schedule_id AND status = '已预约' AND expire_time > NOW();
  IF v_registered + v_appt >= v_max THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '号源已满，无法预约';
  END IF;

  INSERT INTO appointments (
    appointment_no, patient_id, schedule_id, reg_type, appointment_date, expire_time, status
  ) VALUES (
    p_appointment_no, p_patient_id, p_schedule_id, p_reg_type, v_work_date, v_expire, '已预约'
  );

  INSERT INTO operation_logs (user_id, business_type, business_id, operation_content)
  VALUES (p_operator_id, '预约挂号', LAST_INSERT_ID(), CONCAT('创建预约，流水号：', p_appointment_no));

  COMMIT;
END$$

-- 5. 取消预约：仅未取号预约可取消，释放号源
DROP PROCEDURE IF EXISTS sp_cancel_appointment;
CREATE PROCEDURE sp_cancel_appointment(
  IN p_appointment_id INT,
  IN p_operator_id INT,
  IN p_reason VARCHAR(200)
)
BEGIN
  DECLARE v_status VARCHAR(10);
  DECLARE EXIT HANDLER FOR SQLEXCEPTION
  BEGIN
    ROLLBACK;
    RESIGNAL;
  END;

  START TRANSACTION;

  SELECT status INTO v_status
  FROM appointments
  WHERE appointment_id = p_appointment_id
  FOR UPDATE;

  IF v_status IS NULL THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '预约记录不存在';
  END IF;
  IF v_status <> '已预约' THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '仅未取号的预约可以取消';
  END IF;

  UPDATE appointments SET status = '已取消'
  WHERE appointment_id = p_appointment_id;

  INSERT INTO operation_logs (user_id, business_type, business_id, operation_content)
  VALUES (p_operator_id, '取消预约', p_appointment_id, CONCAT('取消预约，原因：', COALESCE(p_reason, '无')));

  COMMIT;
END$$

-- 6. 预约取号：仅就诊当日且未过期可取号，转正式挂号并收费
DROP PROCEDURE IF EXISTS sp_checkin_appointment;
CREATE PROCEDURE sp_checkin_appointment(
  IN p_appointment_id INT,
  IN p_reg_no VARCHAR(40),
  IN p_operator_id INT,
  IN p_pay_method VARCHAR(10)
)
BEGIN
  DECLARE v_patient_id INT;
  DECLARE v_schedule_id INT;
  DECLARE v_reg_type VARCHAR(10);
  DECLARE v_status VARCHAR(10);
  DECLARE v_expire DATETIME;
  DECLARE v_work_date DATE;
  DECLARE v_doctor_id INT;
  DECLARE v_department_id INT;
  DECLARE v_fee DECIMAL(10,2);
  DECLARE v_max INT;
  DECLARE v_registered INT;
  DECLARE v_appt INT;
  DECLARE v_queue INT;
  DECLARE v_registration_id INT;
  DECLARE v_payment_no VARCHAR(40);
  DECLARE EXIT HANDLER FOR SQLEXCEPTION
  BEGIN
    ROLLBACK;
    RESIGNAL;
  END;

  START TRANSACTION;

  SELECT patient_id, schedule_id, reg_type, status, expire_time, appointment_date
  INTO v_patient_id, v_schedule_id, v_reg_type, v_status, v_expire, v_work_date
  FROM appointments
  WHERE appointment_id = p_appointment_id
  FOR UPDATE;

  IF v_patient_id IS NULL THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '预约记录不存在';
  END IF;
  IF v_status <> '已预约' THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '该预约已取号或已取消，不能重复取号';
  END IF;
  IF v_work_date <> CURDATE() THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '仅限就诊当日取号';
  END IF;
  IF v_expire <= NOW() THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '挂号时限已过，预约已过期，请重新挂号';
  END IF;

  SELECT max_registrations, registered_count, schedule_status, doctor_id
  INTO v_max, v_registered, v_status, v_doctor_id
  FROM doctor_schedules
  WHERE schedule_id = v_schedule_id
  FOR UPDATE;

  IF v_status = '停诊' THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '该排班已停诊，不能取号';
  END IF;

  SELECT COUNT(*) INTO v_appt
  FROM appointments
  WHERE schedule_id = v_schedule_id AND status = '已预约' AND expire_time > NOW()
    AND appointment_id <> p_appointment_id;

  IF v_registered + v_appt >= v_max THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '号源已满，无法取号';
  END IF;

  SELECT consultation_fee INTO v_fee FROM doctors WHERE doctor_id = v_doctor_id;
  SELECT department_id INTO v_department_id FROM users u JOIN doctors doc ON u.user_id = doc.user_id WHERE doc.doctor_id = v_doctor_id;

  SET v_queue = v_registered + 1;

  INSERT INTO registrations (
    reg_no, patient_id, doctor_id, department_id, reg_date,
    reg_type, reg_fee, queue_no, visit_status, operator_id
  ) VALUES (
    p_reg_no, v_patient_id, v_doctor_id, v_department_id, CURDATE(),
    v_reg_type, v_fee, v_queue, '待就诊', p_operator_id
  );
  SET v_registration_id = LAST_INSERT_ID();

  UPDATE doctor_schedules
  SET registered_count = registered_count + 1
  WHERE schedule_id = v_schedule_id;

  SET v_payment_no = CONCAT('PAY', DATE_FORMAT(NOW(), '%Y%m%d%H%i%s'), LPAD(v_registration_id, 4, '0'));
  INSERT INTO payments (
    payment_no, patient_id, registration_id, payment_type,
    pay_amount, pay_method, pay_status, operator_id
  ) VALUES (
    v_payment_no, v_patient_id, v_registration_id, '挂号费',
    v_fee, p_pay_method, '已收费', p_operator_id
  );

  UPDATE appointments SET status = '已取号'
  WHERE appointment_id = p_appointment_id;

  INSERT INTO operation_logs (user_id, business_type, business_id, operation_content)
  VALUES (p_operator_id, '预约取号', p_appointment_id, CONCAT('预约取号成功，挂号单号：', p_reg_no));

  COMMIT;
END$$
DELIMITER ;
