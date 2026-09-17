"""Deployment regression tests. No package, service, network, or DB changes."""
import importlib.util
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch


ROOT = Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("cid_deploy_migrate", ROOT / "deploy" / "migrate.py")
migration = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = migration
SPEC.loader.exec_module(migration)


class DeploymentScriptsTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cid-deploy-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.app = self.root / "app"
        shutil.copytree(ROOT / "deploy", self.app / "deploy")
        shutil.copy2(ROOT / "requirements.txt", self.app / "requirements.txt")
        shutil.copy2(ROOT / "schema.sql", self.app / "schema.sql")
        self.config = self.root / "hiworks-sync.env"
        self.units = self.root / "systemd"
        self.units.mkdir()
        self.log = self.root / "calls.log"
        self.bin = self.root / "bin"
        self.bin.mkdir()
        venv_bin = self.app / "venv" / "bin"
        venv_bin.mkdir(parents=True)
        for directory, names in ((self.bin, ["apt-get", "python3", "git", "systemctl", "mysql"]),
                                 (venv_bin, ["pip", "playwright", "python"])):
            for name in names:
                executable = directory / name
                executable.write_text(
                    "#!/bin/bash\n"
                    f"printf '%s\\n' \"{name} $*\" >> \"$CALL_LOG\"\n"
                    + ('exit "${MIGRATION_EXIT:-0}"\n' if name == "python" else "")
                )
                executable.chmod(0o755)
        for name in ("install.sh", "update.sh"):
            script = self.app / "deploy" / name
            script.write_text(script.read_text()
                              .replace("APP=/opt/hiworks", f"APP={self.app}")
                              .replace("ENV=/etc/hiworks-sync.env", f"ENV={self.config}")
                              .replace("/etc/systemd/system/", str(self.units) + "/"))

    def run_script(self, script, *, migration_exit=0):
        return subprocess.run(["bash", str(self.app / "deploy" / script)],
                              env={**os.environ, "PATH": str(self.bin) + os.pathsep + os.environ["PATH"],
                                   "CALL_LOG": str(self.log), "MIGRATION_EXIT": str(migration_exit)},
                              text=True, capture_output=True, check=False)

    def configure(self):
        self.config.write_text("HIWORKS_ID=test@example.invalid\nHIWORKS_PW='test-only-hiworks'\n"
                               "MYSQL_USER=hiworks_sync\nMYSQL_PASSWORD='test-only-db'\n")

    def calls(self):
        return self.log.read_text().splitlines() if self.log.exists() else []

    def test_first_install_creates_private_config_and_stops_before_side_effects(self):
        result = self.run_script("install.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("다시 실행", result.stdout)
        self.assertEqual(self.config.stat().st_mode & 0o777, 0o600)
        self.assertEqual(self.calls(), [])
        self.assertEqual(list(self.units.iterdir()), [])

    def test_placeholder_config_stops_before_package_install(self):
        shutil.copy2(self.app / "deploy" / "hiworks-sync.env.example", self.config)
        result = self.run_script("install.sh")
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("HIWORKS_ID", result.stderr)
        self.assertEqual(self.calls(), [])

    def test_configured_install_initializes_database_before_enabling_lookup(self):
        self.configure()
        result = self.run_script("install.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        migrate = next(i for i, call in enumerate(calls) if "migrate.py --initialize" in call)
        lookup = calls.index("systemctl enable --now hiworks-cidlookup.service")
        self.assertLess(migrate, lookup)
        self.assertNotIn("mysql ", calls)
        self.assertNotIn("test-only-db", result.stdout + result.stderr + "\n".join(calls))

    def test_failed_migration_does_not_restart_or_replace_units(self):
        self.configure()
        result = self.run_script("update.sh", migration_exit=7)
        self.assertEqual(result.returncode, 7)
        self.assertTrue(any("migrate.py" in call for call in self.calls()))
        self.assertFalse(any(call.startswith("systemctl ") for call in self.calls()))
        self.assertEqual(list(self.units.iterdir()), [])

    def test_update_migrates_before_lookup_restart(self):
        self.configure()
        result = self.run_script("update.sh")
        self.assertEqual(result.returncode, 0, result.stderr)
        calls = self.calls()
        migrate = next(i for i, call in enumerate(calls) if "migrate.py" in call)
        self.assertNotIn("--initialize", calls[migrate])
        self.assertLess(migrate, calls.index("systemctl restart hiworks-cidlookup.service"))


class DatabaseMigrationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="cid-migration-test-")
        self.addCleanup(self.temp.cleanup)
        self.settings = migration.Settings("127.0.0.1", 3306, "hiworks_sync", "test-only'password\\",
                                           "review_test", "/tmp/test-only.sock", Path(self.temp.name))
        self.cursor = Mock()
        self.context = Mock()
        self.context.__enter__ = Mock(return_value=self.cursor)
        self.context.__exit__ = Mock(return_value=False)
        self.admin = Mock()
        self.admin.cursor.return_value = self.context
        self.base_columns = [(name,) for name in ("phone", "name", "company", "updated_at")]

    def test_database_identifier_rejected_before_connection(self):
        with patch.dict(os.environ, {"MYSQL_USER": "hiworks_sync", "MYSQL_PASSWORD": "test-only",
                                     "MYSQL_DB": "asterisk`; DROP DATABASE other; --"}, clear=True):
            with self.assertRaises(migration.DeploymentError):
                migration.Settings.from_env()

    def test_separate_database_supported(self):
        with patch.dict(os.environ, {"MYSQL_USER": "hiworks_sync", "MYSQL_PASSWORD": "test-only",
                                     "MYSQL_DB": "review_migration_123"}, clear=True):
            self.assertEqual(migration.Settings.from_env().database, "review_migration_123")

    def test_existing_account_password_mismatch_never_changes_account(self):
        self.cursor.fetchone.return_value = (1,)
        with patch.object(migration, "verify_credentials", side_effect=migration.DeploymentError("mismatch")):
            with self.assertRaises(migration.DeploymentError):
                migration.initialize(self.admin, self.settings)
        self.assertEqual(self.cursor.execute.call_count, 1)
        self.assertTrue(self.cursor.execute.call_args.args[0].startswith("SELECT 1 FROM mysql.user"))

    def test_new_account_password_is_bound_not_interpolated(self):
        self.cursor.fetchone.return_value = None
        migration.initialize(self.admin, self.settings)
        create = next(call for call in self.cursor.execute.call_args_list if call.args[0].startswith("CREATE USER"))
        self.assertEqual(create.args, ("CREATE USER %s@%s IDENTIFIED BY %s",
                                      (self.settings.user, "localhost", self.settings.password)))
        self.assertNotIn(self.settings.password, create.args[0])

    def test_current_schema_is_no_op_without_backup(self):
        self.cursor.fetchall.return_value = self.base_columns + [("grade",)]
        with patch.object(migration, "backup_table") as backup:
            self.assertFalse(migration.migrate(self.admin, self.settings))
        backup.assert_not_called()
        self.assertEqual(self.cursor.execute.call_count, 1)

    def test_missing_grade_is_backed_up_then_added_once(self):
        self.cursor.fetchall.side_effect = [self.base_columns, self.base_columns + [("grade",)]]
        events = []
        self.cursor.execute.side_effect = lambda sql, *args: events.append(sql)
        with patch.object(migration, "backup_table", side_effect=lambda settings: events.append("BACKUP")):
            self.assertTrue(migration.migrate(self.admin, self.settings))
            self.assertFalse(migration.migrate(self.admin, self.settings))
        alters = [event for event in events if event.startswith("ALTER TABLE")]
        self.assertEqual(len(alters), 1)
        self.assertIn("`review_test`.`cid_lookup`", alters[0])
        self.assertLess(events.index("BACKUP"), events.index(alters[0]))

    def test_failed_backup_prevents_alter(self):
        self.cursor.fetchall.return_value = self.base_columns
        with patch.object(migration, "backup_table", side_effect=migration.DeploymentError("backup failed")):
            with self.assertRaises(migration.DeploymentError):
                migration.migrate(self.admin, self.settings)
        self.assertFalse(any(call.args[0].startswith("ALTER") for call in self.cursor.execute.call_args_list))

    def test_backup_is_private_and_contains_dump(self):
        def dump(*args, **kwargs):
            kwargs["stdout"].write(b"-- test-only database dump\n")
        with patch.object(migration.shutil, "which", return_value="/mock/mariadb-dump"), \
             patch.object(migration.subprocess, "run", side_effect=dump) as run:
            backup = migration.backup_table(self.settings)
        self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
        self.assertIn(b"test-only", backup.read_bytes())
        self.assertNotIn(self.settings.password, " ".join(run.call_args.args[0]))

    def test_update_auth_failure_stops_before_admin_changes(self):
        with patch.object(migration, "verify_credentials", side_effect=migration.DeploymentError("auth failed")), \
             patch.object(migration, "connect_admin") as admin:
            with self.assertRaises(migration.DeploymentError):
                migration.deploy_database(self.settings)
        admin.assert_not_called()


if __name__ == "__main__":
    unittest.main()
