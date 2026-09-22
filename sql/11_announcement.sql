-- 11_announcement.sql 公告管理模块（毕设第四章：管理员端-公告管理）
-- 幂等：重复执行先删后建

DROP TABLE IF EXISTS announcements;

CREATE TABLE announcements (
  announcement_id INT AUTO_INCREMENT PRIMARY KEY,
  title VARCHAR(100) NOT NULL COMMENT '公告标题',
  content TEXT NOT NULL COMMENT '公告内容',
  publisher_id INT NOT NULL COMMENT '发布人（用户ID）',
  is_published TINYINT NOT NULL DEFAULT 1 COMMENT '是否发布：1发布 0下线',
  created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
  updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  CONSTRAINT fk_ann_publisher FOREIGN KEY (publisher_id) REFERENCES users (user_id)
) ENGINE = InnoDB DEFAULT CHARSET = utf8mb4 COMMENT = '公告表';

-- 演示公告
INSERT INTO announcements (title, content, publisher_id)
SELECT '门诊系统正式上线通知', '医院门诊管理系统已正式上线运行，支持预约挂号、当日挂号、处方开具、收费退费、检验检查与药房发药等全流程业务，欢迎各科室使用并提出改进建议。', user_id
FROM users WHERE username = 'admin01';

INSERT INTO announcements (title, content, publisher_id)
SELECT '预约挂号功能使用说明', '即日起开通提前预约挂号：请在“预约挂号”中选择就诊日期与排班，提交后生成预约流水号与挂号时限（上午 11:00 / 下午 17:00 前），就诊当日取号转正式挂号。', user_id
FROM users WHERE username = 'admin01';
