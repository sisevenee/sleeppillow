-- 给 sleep_sessions 补 user_id：设备采集的数据必须能归属到受试者。
--
-- 背景：sleep_sessions 原本只有 device_id。user_current_devices 只保存“当前设备”一条，
-- 且设备可以随时切换，因此设备轮换或多人共用时会话无法归属到受试者，历史归属也不可追溯。
--
-- 本迁移只做结构与索引变更，不改动任何已有业务数据。
-- 历史行回填请用 backfill_session_user_id.py（先 --dry-run 看清楚再执行）。
--
-- 幂等：可在已有该列的库上重复执行。

USE pillow;

-- 1) 新增 user_id。允许 NULL，因为 ESP32 可能在 App 登录或选择设备之前就已经在上传数据。
--    查询侧一律用 COALESCE(s.user_id, 该设备当前用户) 兜底，所以 NULL 不会导致数据消失。
SET @has_column := (
    SELECT COUNT(*) FROM information_schema.COLUMNS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sleep_sessions' AND COLUMN_NAME = 'user_id'
);
SET @sql := IF(@has_column = 0,
    'ALTER TABLE sleep_sessions ADD COLUMN user_id BIGINT UNSIGNED NULL AFTER device_id',
    'DO 0'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 2) 外键。删用户时保留睡眠数据，因此用 ON DELETE SET NULL 而不是 CASCADE：
--    受试者退出不应该连带删掉已经采集到的整夜数据。
SET @has_fk := (
    SELECT COUNT(*) FROM information_schema.TABLE_CONSTRAINTS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sleep_sessions'
      AND CONSTRAINT_NAME = 'fk_sleep_sessions_user'
);
SET @sql := IF(@has_fk = 0,
    'ALTER TABLE sleep_sessions ADD CONSTRAINT fk_sleep_sessions_user FOREIGN KEY (user_id) REFERENCES users (id) ON DELETE SET NULL',
    'DO 0'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

-- 3) 查询索引：按受试者列出睡眠夜、按受试者导出都要走这个索引。
SET @has_index := (
    SELECT COUNT(*) FROM information_schema.STATISTICS
    WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sleep_sessions' AND INDEX_NAME = 'idx_sleep_sessions_user_ended'
);
SET @sql := IF(@has_index = 0,
    'ALTER TABLE sleep_sessions ADD KEY idx_sleep_sessions_user_ended (user_id, ended_at)',
    'DO 0'
);
PREPARE stmt FROM @sql;
EXECUTE stmt;
DEALLOCATE PREPARE stmt;

SELECT COLUMN_NAME, COLUMN_TYPE, IS_NULLABLE
FROM information_schema.COLUMNS
WHERE TABLE_SCHEMA = DATABASE() AND TABLE_NAME = 'sleep_sessions'
ORDER BY ORDINAL_POSITION;
