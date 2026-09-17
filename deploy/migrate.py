#!/usr/bin/env python3
"""Local MariaDB bootstrap and additive schema migration, run before service restart."""
import argparse
from dataclasses import dataclass
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile


class DeploymentError(Exception):
    pass


@dataclass(frozen=True)
class Settings:
    host: str
    port: int
    user: str
    password: str
    database: str
    admin_socket: str
    backup_dir: Path

    @classmethod
    def from_env(cls):
        host = os.getenv("MYSQL_HOST", "127.0.0.1")
        try:
            port = int(os.getenv("MYSQL_PORT", "3306"))
        except ValueError as exc:
            raise DeploymentError("MYSQL_PORT는 정수여야 합니다.") from exc
        if host not in {"127.0.0.1", "localhost", "::1"} or port != 3306:
            raise DeploymentError("이 배포 도구는 로컬 MariaDB(3306)용입니다. 원격 DB는 관리자에게 마이그레이션을 요청하세요.")
        user = os.getenv("MYSQL_USER", "")
        password = os.getenv("MYSQL_PASSWORD", "")
        if not user or not password or password in {"여기에_MySQL_비밀번호", "changeme_sync", "CHANGE_ME", "********"}:
            raise DeploymentError("MYSQL_USER/MYSQL_PASSWORD의 실제 값을 설정하세요.")
        database = os.getenv("MYSQL_DB", "asterisk")
        if not re.fullmatch(r"[A-Za-z0-9_]{1,64}", database):
            raise DeploymentError("MYSQL_DB에는 영문, 숫자, 밑줄만 사용할 수 있습니다(최대 64자).")
        return cls(host, port, user, password, database,
                   os.getenv("MYSQL_ADMIN_SOCKET", "/run/mysqld/mysqld.sock"),
                   Path(os.getenv("MIGRATION_BACKUP_DIR", "/var/backups/hiworks-cid")))


def connect_admin(settings):
    import pymysql
    return pymysql.connect(user="root", unix_socket=settings.admin_socket,
                           charset="utf8mb4", autocommit=True,
                           connect_timeout=5, read_timeout=10, write_timeout=10)


def verify_credentials(settings, *, database=True, schema=False):
    import pymysql
    try:
        conn = pymysql.connect(host=settings.host, port=settings.port,
                               user=settings.user, password=settings.password,
                               database=settings.database if database else None,
                               charset="utf8mb4", autocommit=True,
                               connect_timeout=5, read_timeout=10, write_timeout=10)
        try:
            with conn.cursor() as cur:
                cur.execute("SELECT phone, name, company, grade FROM cid_lookup LIMIT 0" if schema else "SELECT 1")
        finally:
            conn.close()
    except pymysql.MySQLError as exc:
        raise DeploymentError(
            "앱 DB 인증/권한/스키마 검증에 실패했습니다. 기존 계정의 비밀번호는 변경하지 않았습니다. "
            "DB 실행 상태와 env의 계정/비밀번호/DB를 확인하세요. 비밀번호를 잃어버렸다면 "
            "DB 관리자가 명시적으로 재설정한 뒤 env도 같은 값으로 변경해야 합니다(README 갱신 절차)."
        ) from exc


def initialize(admin, settings):
    """Create only missing accounts; never reset an existing account password."""
    with admin.cursor() as cur:
        cur.execute("SELECT 1 FROM mysql.user WHERE User=%s AND Host=%s", (settings.user, "localhost"))
        exists = cur.fetchone() is not None
    if exists:
        # Check before any schema or grant change, even when the database is new.
        verify_credentials(settings, database=False)
    with admin.cursor() as cur:
        cur.execute(f"CREATE DATABASE IF NOT EXISTS `{settings.database}` CHARACTER SET utf8mb4 COLLATE utf8mb4_unicode_ci")
        # Keep initial table definitions in schema.sql; execute only its CREATE TABLE.
        schema = (Path(__file__).resolve().parents[1] / "schema.sql").read_text()
        match = re.search(r"CREATE TABLE IF NOT EXISTS cid_lookup\s*\(.*?;", schema, re.DOTALL)
        if not match:
            raise DeploymentError("schema.sql에서 cid_lookup 테이블 정의를 찾지 못했습니다.")
        cur.execute(match.group().replace("cid_lookup", f"`{settings.database}`.`cid_lookup`", 1))
        if not exists:
            cur.execute("CREATE USER %s@%s IDENTIFIED BY %s", (settings.user, "localhost", settings.password))
        cur.execute(f"GRANT SELECT,INSERT,UPDATE,DELETE ON `{settings.database}`.`cid_lookup` TO %s@%s",
                    (settings.user, "localhost"))


def backup_table(settings):
    executable = shutil.which("mariadb-dump") or shutil.which("mysqldump")
    if not executable:
        raise DeploymentError("마이그레이션 전 백업에 필요한 mariadb-dump/mysqldump가 없습니다.")
    settings.backup_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
    with tempfile.NamedTemporaryFile(prefix=f"{settings.database}-before-grade-", suffix=".sql",
                                     dir=settings.backup_dir, delete=False) as output:
        backup = Path(output.name)
        try:
            subprocess.run([executable, "--user=root", f"--socket={settings.admin_socket}",
                            "--single-transaction", "--skip-lock-tables", "--",
                            settings.database, "cid_lookup"], stdout=output,
                           stderr=subprocess.PIPE, check=True, timeout=60)
            if output.tell() == 0:
                raise DeploymentError("빈 DB 백업이 생성되어 마이그레이션을 중단했습니다.")
        except (OSError, subprocess.SubprocessError, DeploymentError) as exc:
            backup.unlink(missing_ok=True)
            raise DeploymentError("DB 백업에 실패하여 스키마를 변경하지 않았습니다.") from exc
    print(f"마이그레이션 전 백업: {backup}")
    return backup


def migrate(admin, settings):
    with admin.cursor() as cur:
        cur.execute("SELECT COLUMN_NAME FROM information_schema.COLUMNS WHERE TABLE_SCHEMA=%s AND TABLE_NAME=%s",
                    (settings.database, "cid_lookup"))
        columns = {row[0] for row in cur.fetchall()}
    if not {"phone", "name", "company", "updated_at"}.issubset(columns):
        raise DeploymentError("cid_lookup 기본 스키마가 없거나 예상과 다릅니다. 신규 설치는 install.sh를 사용하세요.")
    if "grade" in columns:
        print("DB 스키마 최신 상태: 변경 없음.")
        return False
    backup_table(settings)
    with admin.cursor() as cur:
        cur.execute(f"ALTER TABLE `{settings.database}`.`cid_lookup` ADD COLUMN `grade` VARCHAR(100) DEFAULT NULL AFTER `company`")
    print("DB 마이그레이션 완료: grade 컬럼 추가.")
    return True


def deploy_database(settings, *, create=False):
    if not create:
        verify_credentials(settings)
    admin = connect_admin(settings)
    try:
        if create:
            initialize(admin, settings)
            verify_credentials(settings)
        migrate(admin, settings)
        verify_credentials(settings, schema=True)
    finally:
        admin.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--initialize", action="store_true", help="create missing schema/account before migration")
    args = parser.parse_args()
    try:
        deploy_database(Settings.from_env(), create=args.initialize)
    except DeploymentError as exc:
        print(f"배포 중단: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:
        # Do not include server error text, which can contain SQL or credentials.
        print(f"배포 중단: DB 관리 작업 실패({type(exc).__name__}). root 소켓 인증과 DB 상태를 확인하세요.", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
