-- Asterisk CID lookup table (MySQL / MariaDB)
CREATE DATABASE IF NOT EXISTS asterisk CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE asterisk;

CREATE TABLE IF NOT EXISTS cid_lookup (
  phone      VARCHAR(20)  NOT NULL,          -- 숫자만 저장된 정규화 번호 (예: 01012345678)
  name       VARCHAR(255) NOT NULL,
  company    VARCHAR(255) DEFAULT NULL,
  updated_at TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (phone)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 조회 전용 계정 (Asterisk가 사용) — 비밀번호는 직접 바꾸세요
-- CREATE USER 'asterisk_ro'@'localhost' IDENTIFIED BY 'CHANGE_ME';
-- GRANT SELECT ON asterisk.cid_lookup TO 'asterisk_ro'@'localhost';
-- 동기화 스크립트용 계정
-- CREATE USER 'hiworks_sync'@'localhost' IDENTIFIED BY 'CHANGE_ME';
-- GRANT SELECT, INSERT, UPDATE, DELETE ON asterisk.cid_lookup TO 'hiworks_sync'@'localhost';
-- FLUSH PRIVILEGES;
