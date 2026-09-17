CREATE DATABASE IF NOT EXISTS pillow
  CHARACTER SET utf8mb4
  COLLATE utf8mb4_unicode_ci;

USE pillow;

CREATE TABLE IF NOT EXISTS devices (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    device_id VARCHAR(64) NOT NULL,
    display_name VARCHAR(128) NOT NULL,
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_devices_device_id (device_id)
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS device_credentials (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    device_id BIGINT UNSIGNED NOT NULL,
    token_hash CHAR(64) NOT NULL,
    label VARCHAR(128) NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at DATETIME NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_device_credentials_token_hash (token_hash),
    KEY idx_device_credentials_active (device_id, revoked_at),
    CONSTRAINT fk_device_credentials_device
      FOREIGN KEY (device_id) REFERENCES devices (id) ON DELETE CASCADE
) ENGINE=InnoDB;

CREATE TABLE IF NOT EXISTS telemetry (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    device_id BIGINT UNSIGNED NOT NULL,
    sampled_at DATETIME NOT NULL,
    heart_rate DOUBLE NOT NULL,
    respiratory_rate DOUBLE NOT NULL,
    temperature DOUBLE NOT NULL,
    sleep_stage VARCHAR(64) NOT NULL,
    confidence DOUBLE NOT NULL,
    time_source VARCHAR(32) NULL,
    analog_voltage DOUBLE NULL,
    fpga_input TINYINT NULL,
    received_at DATETIME NOT NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_telemetry_device_sample (device_id, sampled_at),
    KEY idx_telemetry_device_sampled_at (device_id, sampled_at),
    CONSTRAINT fk_telemetry_device
      FOREIGN KEY (device_id) REFERENCES devices (id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- 一次连续的睡眠枕监测会话由 ESP32 上传的连续数据自动创建和结束。首条有效采样
-- 代表设备上电后开始工作；last_sample_at 用于判断设备断电或停止采集：连续 15 分钟
-- 没有新数据时，后端以最后一条数据为结束时间。
-- user_id 记录“该时刻正在使用这台设备的受试者”，是会话归属链的关键字段。
-- 允许 NULL：ESP32 可能在 App 登录或选择设备之前就已经在上传数据，此时无法确定归属。
-- 查询侧统一用 COALESCE(s.user_id, 该设备当前用户) 兜底，因此 NULL 不会导致数据消失。
-- 外键用 ON DELETE SET NULL：受试者退出不应连带删除已采集到的整夜数据。
CREATE TABLE IF NOT EXISTS sleep_sessions (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    device_id BIGINT UNSIGNED NOT NULL,
    user_id BIGINT UNSIGNED NULL,
    started_at DATETIME NOT NULL,
    ended_at DATETIME NULL,
    last_sample_at DATETIME NULL,
    start_source ENUM('app', 'telemetry') NOT NULL,
    end_source ENUM('app', 'data_gap') NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    closed_at DATETIME NULL,
    PRIMARY KEY (id),
    KEY idx_sleep_sessions_device_active (device_id, ended_at, last_sample_at),
    KEY idx_sleep_sessions_device_ended (device_id, ended_at),
    KEY idx_sleep_sessions_user_ended (user_id, ended_at),
    CONSTRAINT fk_sleep_sessions_device
      FOREIGN KEY (device_id) REFERENCES devices (id) ON DELETE CASCADE,
    CONSTRAINT fk_sleep_sessions_user
      FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE SET NULL
) ENGINE=InnoDB;

-- App 登录账号。普通账号仅用于登录和保存当前选择的设备；管理员账号可查看全部设备。
CREATE TABLE IF NOT EXISTS users (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    username VARCHAR(64) NOT NULL,
    password_hash VARCHAR(255) NOT NULL,
    role ENUM('admin', 'user') NOT NULL DEFAULT 'user',
    is_active TINYINT(1) NOT NULL DEFAULT 1,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_users_username (username)
) ENGINE=InnoDB;

-- 用户在 App 中点击“准备入睡”或“结束准备入睡”时的主观时间标记。
-- 它们用于问卷与实验分析，不改变由设备上电/断电决定的 sleep_sessions 边界。
CREATE TABLE IF NOT EXISTS sleep_manual_markers (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id BIGINT UNSIGNED NOT NULL,
    device_id BIGINT UNSIGNED NOT NULL,
    marker_type ENUM('prepare', 'wake') NOT NULL,
    marked_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    KEY idx_sleep_manual_markers_user_time (user_id, marked_at),
    KEY idx_sleep_manual_markers_device_time (device_id, marked_at),
    CONSTRAINT fk_sleep_manual_markers_user
      FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
    CONSTRAINT fk_sleep_manual_markers_device
      FOREIGN KEY (device_id) REFERENCES devices (id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- 每个普通账号只保存“当前通过蓝牙选择的设备”，不预先固定分配，也可随时切换。
CREATE TABLE IF NOT EXISTS user_current_devices (
    user_id BIGINT UNSIGNED NOT NULL,
    device_id BIGINT UNSIGNED NOT NULL,
    selected_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (user_id),
    KEY idx_user_current_devices_device (device_id),
    CONSTRAINT fk_user_current_devices_user
      FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE,
    CONSTRAINT fk_user_current_devices_device
      FOREIGN KEY (device_id) REFERENCES devices (id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- 手机 App 与管理员网页使用的登录会话。数据库只保存令牌哈希，不保存明文令牌。
CREATE TABLE IF NOT EXISTS auth_sessions (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id BIGINT UNSIGNED NOT NULL,
    token_hash CHAR(64) NOT NULL,
    expires_at DATETIME NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    revoked_at DATETIME NULL,
    PRIMARY KEY (id),
    UNIQUE KEY uq_auth_sessions_token_hash (token_hash),
    KEY idx_auth_sessions_active (user_id, expires_at, revoked_at),
    CONSTRAINT fk_auth_sessions_user
      FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- App 内问卷的真实提交记录。每日问卷按中国日期保存一份；PSQI 前测、后测
-- 各有独立类型，因此可以长期保留并在管理员后台分别查看。
-- answers_json 保存题号到答案的 JSON，避免把姓名、主观描述等敏感答案拆散到日志中。
CREATE TABLE IF NOT EXISTS questionnaire_submissions (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id BIGINT UNSIGNED NOT NULL,
    questionnaire_type VARCHAR(32) NOT NULL,
    response_date DATE NOT NULL,
    questionnaire_version VARCHAR(16) NOT NULL DEFAULT 'v1',
    answers_json MEDIUMTEXT NOT NULL,
    submitted_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_questionnaire_submission_day (user_id, questionnaire_type, response_date),
    KEY idx_questionnaire_submissions_date (response_date, questionnaire_type),
    KEY idx_questionnaire_submissions_user (user_id, submitted_at),
    CONSTRAINT fk_questionnaire_submissions_user
      FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB;

-- 条件题中的图片单独保存，避免把二进制内容塞进 answers_json。单张图片由 API
-- 限制在 10 MB 以内；只有提交者本人或管理员能通过已登录接口读取。
CREATE TABLE IF NOT EXISTS questionnaire_attachments (
    id BIGINT UNSIGNED NOT NULL AUTO_INCREMENT,
    user_id BIGINT UNSIGNED NOT NULL,
    questionnaire_type VARCHAR(32) NOT NULL,
    response_date DATE NOT NULL,
    attachment_key VARCHAR(64) NOT NULL,
    content_type VARCHAR(32) NOT NULL,
    content_length INT UNSIGNED NOT NULL,
    content_sha256 CHAR(64) NOT NULL,
    image_data MEDIUMBLOB NOT NULL,
    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    updated_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
    PRIMARY KEY (id),
    UNIQUE KEY uq_questionnaire_attachment (user_id, questionnaire_type, response_date, attachment_key),
    KEY idx_questionnaire_attachment_lookup (user_id, questionnaire_type, response_date),
    CONSTRAINT fk_questionnaire_attachments_user
      FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE CASCADE
) ENGINE=InnoDB;
