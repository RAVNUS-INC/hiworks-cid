-- Asterisk CID lookup table (MySQL / MariaDB)
CREATE DATABASE IF NOT EXISTS asterisk CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci;

USE asterisk;

CREATE TABLE IF NOT EXISTS cid_lookup (
  phone      VARCHAR(20)  NOT NULL,          -- 숫자만 저장된 정규화 번호 (예: 01012345678)
  name       VARCHAR(255) NOT NULL,
  company    VARCHAR(255) DEFAULT NULL,
  grade      VARCHAR(100) DEFAULT NULL,       -- 직급 (예: 부장). CID 표시에 사용
  updated_at TIMESTAMP    DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (phone)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- Hiworks 공유주소록 원본 미러. 목록/상세 원문도 함께 보존해 새 필드가 추가돼도
-- 동기화 코드가 갱신되기 전까지 데이터가 유실되지 않게 한다.
CREATE TABLE IF NOT EXISTS hiworks_contacts (
  contact_type          VARCHAR(50)  NOT NULL,
  contact_no            BIGINT       NOT NULL,
  owner                 BOOLEAN      NOT NULL DEFAULT FALSE,
  name                  VARCHAR(255) NOT NULL,
  company               VARCHAR(255) DEFAULT NULL,
  department            VARCHAR(255) DEFAULT NULL,
  grade                 VARCHAR(100) DEFAULT NULL,
  homepage              TEXT         DEFAULT NULL,
  birth                 VARCHAR(100) DEFAULT NULL,
  memo                  TEXT         DEFAULT NULL,
  image                 TEXT         DEFAULT NULL,
  calendar_type         VARCHAR(50)  DEFAULT NULL,
  allow_editing         BOOLEAN      DEFAULT NULL,
  is_star               BOOLEAN      DEFAULT NULL,
  is_owner              BOOLEAN      DEFAULT NULL,
  updater               VARCHAR(255) DEFAULT NULL,
  source_created_at     VARCHAR(100) DEFAULT NULL,
  source_updated_at     VARCHAR(100) DEFAULT NULL,
  list_json             LONGTEXT     NOT NULL,
  detail_json           LONGTEXT     NOT NULL,
  synced_at             TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (contact_type, contact_no)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS hiworks_contact_phones (
  contact_type      VARCHAR(50)  NOT NULL,
  contact_no        BIGINT       NOT NULL,
  ordinal_no        INT UNSIGNED NOT NULL,
  phone_type        VARCHAR(100) NOT NULL,
  phone_raw         VARCHAR(255) NOT NULL,
  phone_normalized  VARCHAR(20)  DEFAULT NULL,
  is_default        BOOLEAN      NOT NULL,
  raw_json          LONGTEXT     NOT NULL,
  PRIMARY KEY (contact_type, contact_no, ordinal_no),
  KEY idx_hiworks_phone_normalized (phone_normalized),
  CONSTRAINT fk_hiworks_phone_contact FOREIGN KEY (contact_type, contact_no)
    REFERENCES hiworks_contacts (contact_type, contact_no) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS hiworks_contact_emails (
  contact_type  VARCHAR(50)  NOT NULL,
  contact_no    BIGINT       NOT NULL,
  ordinal_no    INT UNSIGNED NOT NULL,
  email         VARCHAR(320) NOT NULL,
  is_default    BOOLEAN      NOT NULL,
  raw_json      LONGTEXT     NOT NULL,
  PRIMARY KEY (contact_type, contact_no, ordinal_no),
  KEY idx_hiworks_email (email),
  CONSTRAINT fk_hiworks_email_contact FOREIGN KEY (contact_type, contact_no)
    REFERENCES hiworks_contacts (contact_type, contact_no) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS hiworks_contact_addresses (
  contact_type  VARCHAR(50)  NOT NULL,
  contact_no    BIGINT       NOT NULL,
  ordinal_no    INT UNSIGNED NOT NULL,
  value_text    TEXT         DEFAULT NULL,
  raw_json      LONGTEXT     NOT NULL,
  PRIMARY KEY (contact_type, contact_no, ordinal_no),
  CONSTRAINT fk_hiworks_address_contact FOREIGN KEY (contact_type, contact_no)
    REFERENCES hiworks_contacts (contact_type, contact_no) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

CREATE TABLE IF NOT EXISTS hiworks_contact_tags (
  contact_type  VARCHAR(50)  NOT NULL,
  contact_no    BIGINT       NOT NULL,
  ordinal_no    INT UNSIGNED NOT NULL,
  value_text    TEXT         DEFAULT NULL,
  raw_json      LONGTEXT     NOT NULL,
  PRIMARY KEY (contact_type, contact_no, ordinal_no),
  CONSTRAINT fk_hiworks_tag_contact FOREIGN KEY (contact_type, contact_no)
    REFERENCES hiworks_contacts (contact_type, contact_no) ON DELETE CASCADE
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 조직도 API는 현재 CID 생성에도 사용한다. 구조가 바뀌더라도 전체 응답을 보존한다.
CREATE TABLE IF NOT EXISTS hiworks_organization_snapshot (
  snapshot_id  TINYINT      NOT NULL DEFAULT 1,
  raw_json     LONGTEXT     NOT NULL,
  synced_at    TIMESTAMP    NOT NULL DEFAULT CURRENT_TIMESTAMP ON UPDATE CURRENT_TIMESTAMP,
  PRIMARY KEY (snapshot_id)
) ENGINE=InnoDB DEFAULT CHARSET=utf8mb4 COLLATE=utf8mb4_unicode_ci;

-- 조회 전용 계정 (Asterisk가 사용) — 비밀번호는 직접 바꾸세요
-- CREATE USER 'asterisk_ro'@'localhost' IDENTIFIED BY 'CHANGE_ME';
-- GRANT SELECT ON asterisk.cid_lookup TO 'asterisk_ro'@'localhost';
-- 동기화 스크립트용 계정
-- CREATE USER 'hiworks_sync'@'localhost' IDENTIFIED BY 'CHANGE_ME';
-- 동기화 계정에는 위 7개 테이블에 대한 SELECT/INSERT/UPDATE/DELETE 권한을 부여합니다.
-- FLUSH PRIVILEGES;
