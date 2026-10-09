-- 13_medical_records.sql 门诊病历模块
-- 毕设第四章：医生端-门诊病历书写
--
-- 设计说明：
--   现有 patients.diagnosis 只是一个 VARCHAR(500) 单字段，且**每次就诊覆盖写**，
--   无法保留历次就诊记录，也不区分主诉/现病史/体格检查/医嘱，不构成真正意义的病历。
--   本脚本新增 medical_records 表，**按「一次就诊」一条**记录：
--     * 与 registrations(挂号记录) 一对一 → 同一患者多次就诊各有各的病历；
--     * 诊断采用「主诊断 + 诊断编码」结构，便于报表统计病种；
--     * 保留 patients.diagnosis 作为「最近一次诊断」的冗余快照，
--       病历保存时由应用层同步更新，兼容既有诊断管理与报表查询。
--
-- 幂等：重复执行先删后建（与 11_announcement.sql 同一风格）

USE hospital_outpatient;

DROP TABLE IF EXISTS medical_records;

CREATE TABLE medical_records (
  record_id INT AUTO_INCREMENT PRIMARY KEY COMMENT '病历编号',
  record_no VARCHAR(40) NOT NULL UNIQUE COMMENT '病历号',
  registration_id INT NOT NULL UNIQUE COMMENT '关联挂号记录（一次就诊一份病历）',
  patient_id INT NOT NULL COMMENT '患者编号',
  doctor_id INT NOT NULL COMMENT '接诊医生编号',
  department_id INT NOT NULL COMMENT '就诊科室编号',
  visit_date DATE NOT NULL COMMENT '就诊日期',

  -- 主诉：患者本次就诊最主要的症状与持续时间
  chief_complaint VARCHAR(500) NULL COMMENT '主诉',

  -- 现病史：本次发病的起因、经过、演变与诊疗经过
  present_illness TEXT NULL COMMENT '现病史',

  -- 既往史：既往疾病、手术、过敏、用药史（默认带出患者档案的既往病史）
  past_history TEXT NULL COMMENT '既往史',

  -- 体格检查：生命体征与阳性体征（体温/脉搏/呼吸/血压/专科查体）
  physical_exam TEXT NULL COMMENT '体格检查',

  -- 诊断：主诊断 + 诊断编码（ICD 风格编码，便于统计病种分布）
  diagnosis VARCHAR(500) NULL COMMENT '诊断结果（主诊断）',
  diagnosis_code VARCHAR(30) NULL COMMENT '诊断编码（ICD-10 风格）',

  -- 医嘱：用药与处置嘱咐
  advice TEXT NULL COMMENT '医嘱/处置意见',

  -- 病历状态：草稿可反复修改，已归档表示本次就诊病历定稿
  record_status ENUM('草稿','已归档') NOT NULL DEFAULT '草稿' COMMENT '病历状态',

  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT '创建时间',
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP COMMENT '更新时间',

  CONSTRAINT fk_mr_registration FOREIGN KEY (registration_id) REFERENCES registrations (registration_id),
  CONSTRAINT fk_mr_patient FOREIGN KEY (patient_id) REFERENCES patients (patient_id),
  CONSTRAINT fk_mr_doctor FOREIGN KEY (doctor_id) REFERENCES doctors (doctor_id),
  CONSTRAINT fk_mr_department FOREIGN KEY (department_id) REFERENCES departments (department_id),
  CONSTRAINT ck_mr_visit CHECK (visit_date IS NOT NULL)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '门诊病历表';

-- 常用查询索引：按患者查历次病历 / 按医生查接诊量 / 按日期查就诊量
CREATE INDEX idx_mr_patient ON medical_records (patient_id, visit_date);
CREATE INDEX idx_mr_doctor ON medical_records (doctor_id, visit_date);
CREATE INDEX idx_mr_dept ON medical_records (department_id, visit_date);
CREATE INDEX idx_mr_status ON medical_records (record_status);

-- ============================================================
-- 病历号生成：MR + 日期 + 4 位序号（与挂号单号风格一致，由应用层调用）
-- ============================================================
DROP PROCEDURE IF EXISTS sp_next_record_no;
DELIMITER $$
CREATE PROCEDURE sp_next_record_no(OUT p_record_no VARCHAR(40))
BEGIN
  DECLARE v_prefix VARCHAR(20);
  DECLARE v_seq INT;

  SET v_prefix = CONCAT('MR', DATE_FORMAT(CURDATE(), '%Y%m%d'));

  SELECT IFNULL(MAX(CAST(SUBSTRING(record_no, LENGTH(v_prefix) + 1) AS UNSIGNED)), 0) + 1
    INTO v_seq
  FROM medical_records
  WHERE record_no LIKE CONCAT(v_prefix, '%');

  SET p_record_no = CONCAT(v_prefix, LPAD(v_seq, 4, '0'));
END$$
DELIMITER ;

-- ============================================================
-- 保存病历：写入/更新病历，并同步患者档案的「最近一次诊断」冗余字段
--   p_record_id = 0 或 NULL 表示新增，否则更新
-- 事务保证：病历与患者档案诊断快照要么一起成功，要么一起回滚
-- ============================================================
DROP PROCEDURE IF EXISTS sp_save_medical_record;
DELIMITER $$
CREATE PROCEDURE sp_save_medical_record(
  IN p_record_id INT,
  IN p_registration_id INT,
  IN p_chief_complaint VARCHAR(500),
  IN p_present_illness TEXT,
  IN p_past_history TEXT,
  IN p_physical_exam TEXT,
  IN p_diagnosis VARCHAR(500),
  IN p_diagnosis_code VARCHAR(30),
  IN p_advice TEXT,
  IN p_record_status VARCHAR(10),
  OUT p_result_id INT
)
BEGIN
  DECLARE v_patient_id INT;
  DECLARE v_doctor_id INT;
  DECLARE v_department_id INT;
  DECLARE v_visit_date DATE;
  DECLARE v_record_no VARCHAR(40);
  DECLARE v_exists INT;

  DECLARE EXIT HANDLER FOR SQLEXCEPTION
  BEGIN
    ROLLBACK;
    RESIGNAL;
  END;

  START TRANSACTION;

  -- 取出挂号记录上的患者/医生/科室/就诊日期，防止调用方传错
  SELECT patient_id, doctor_id, department_id, reg_date
    INTO v_patient_id, v_doctor_id, v_department_id, v_visit_date
  FROM registrations
  WHERE registration_id = p_registration_id;

  IF v_patient_id IS NULL THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '挂号记录不存在，无法书写病历';
  END IF;

  -- 同一挂号记录只允许一份病历
  SELECT COUNT(*) INTO v_exists
  FROM medical_records
  WHERE registration_id = p_registration_id
    AND (p_record_id IS NULL OR p_record_id = 0 OR record_id <> p_record_id);

  IF v_exists > 0 THEN
    SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = '该次就诊已有病历，请直接编辑已有病历';
  END IF;

  IF p_record_id IS NULL OR p_record_id = 0 THEN
    -- 新增：生成病历号
    CALL sp_next_record_no(v_record_no);

    INSERT INTO medical_records (
      record_no, registration_id, patient_id, doctor_id, department_id, visit_date,
      chief_complaint, present_illness, past_history, physical_exam,
      diagnosis, diagnosis_code, advice, record_status
    ) VALUES (
      v_record_no, p_registration_id, v_patient_id, v_doctor_id, v_department_id, v_visit_date,
      p_chief_complaint, p_present_illness, p_past_history, p_physical_exam,
      p_diagnosis, p_diagnosis_code, p_advice, IFNULL(p_record_status, '草稿')
    );

    SET p_result_id = LAST_INSERT_ID();
  ELSE
    UPDATE medical_records
    SET chief_complaint = p_chief_complaint,
        present_illness = p_present_illness,
        past_history    = p_past_history,
        physical_exam   = p_physical_exam,
        diagnosis       = p_diagnosis,
        diagnosis_code  = p_diagnosis_code,
        advice          = p_advice,
        record_status   = IFNULL(p_record_status, '草稿')
    WHERE record_id = p_record_id;

    SET p_result_id = p_record_id;
  END IF;

  -- 同步患者档案的「最近一次诊断」快照（兼容既有诊断管理与报表）
  IF p_diagnosis IS NOT NULL AND p_diagnosis <> '' THEN
    UPDATE patients SET diagnosis = p_diagnosis WHERE patient_id = v_patient_id;
  END IF;

  COMMIT;
END$$
DELIMITER ;
