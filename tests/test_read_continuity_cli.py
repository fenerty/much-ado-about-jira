import importlib.util
import json
import sqlite3
import subprocess
import sys
from pathlib import Path

import pytest

from tests.test_cache_scopes import ACCOUNT, SITE, binding, snapshot
from tests.test_legacy_read_continuity import advance, current_store, mature_legacy


SCRIPT = Path(__file__).resolve().parents[1] / 'scripts' / 'restore-legacy-read-state.py'
TABLES = ('entities', 'local_state', 'connector_runs', 'app_meta')


def database_rows(path):
    with sqlite3.connect(path) as connection:
        return {
            table: connection.execute(f'SELECT * FROM {table} ORDER BY 1').fetchall()
            for table in TABLES
        }


@pytest.fixture
def continuity_cache(tmp_path, advance):
    path = tmp_path / 'cache.db'
    mature_legacy(path, advance)
    empty = snapshot().model_copy(update={'work_items': [], 'activities': []})
    store = current_store(path, advance, result=empty)
    advance()
    store.replace_connector(snapshot(), 60)
    assert store.load()[1][0].unread
    config = tmp_path / 'settings.toml'
    config.write_text(
        '[azure_devops]\nenabled = false\n[jira]\n'
        f'site = "{SITE}"\nexpected_account = "{ACCOUNT}"\nactivity_projects = ["ENG"]\n',
        encoding='utf-8',
    )
    arguments = [
        '--config', str(config), '--database', str(path),
        '--connector', 'jira', '--scope', binding()['scope'],
    ]
    return path, store, arguments


def run_cli(arguments):
    return subprocess.run(
        [sys.executable, str(SCRIPT), *arguments],
        capture_output=True, text=True, encoding='utf-8', timeout=30,
    )


def test_preview_is_read_only_and_apply_restores_reads_after_a_complete_wal_backup(
    tmp_path, continuity_cache
):
    path, store, arguments = continuity_cache
    backup = tmp_path / 'before-migration.db'
    # Keep a committed row in the live WAL so copying only the main file
    # cannot satisfy the backup assertion.
    keeper = sqlite3.connect(path)
    try:
        keeper.execute('PRAGMA wal_autocheckpoint = 0')
        keeper.execute("INSERT INTO app_meta VALUES ('cli-test:wal', 'retained')")
        keeper.commit()
        assert Path(str(path) + '-wal').stat().st_size > 0
        before = database_rows(path)

        preview = run_cli(arguments)
        assert preview.returncode == 0, preview.stderr
        result = json.loads(preview.stdout)
        assert result['applied'] is False and result['backup'] is None
        assert result['counts']['read'] == 1
        assert database_rows(path) == before
        assert store.load()[1][0].unread
        assert not backup.exists()

        applied = run_cli([*arguments, '--apply', '--backup', str(backup)])
        assert applied.returncode == 0, applied.stderr
        result = json.loads(applied.stdout)
        assert result['applied'] is True and result['backup'] == str(backup)
        assert result['counts']['read'] == 1
        with sqlite3.connect(backup) as connection:
            assert connection.execute('PRAGMA integrity_check').fetchall() == [('ok',)]
        assert database_rows(backup) == before
        assert not store.load()[1][0].unread
        with sqlite3.connect(path) as connection:
            assert connection.execute(
                "SELECT value FROM app_meta WHERE key = 'legacy_read_scope:jira'"
            ).fetchone() == (f"jira@{binding()['scope']}",)
    finally:
        keeper.close()


def test_existing_backup_is_not_overwritten_and_apply_does_not_change_database(
    tmp_path, continuity_cache
):
    path, store, arguments = continuity_cache
    backup = tmp_path / 'existing-backup.db'
    original = b'Keep this pre-existing backup unchanged'
    backup.write_bytes(original)
    before = database_rows(path)

    applied = run_cli([*arguments, '--apply', '--backup', str(backup)])

    assert applied.returncode == 1
    assert 'Read-state continuity failed:' in applied.stderr
    assert backup.read_bytes() == original
    assert database_rows(path) == before
    assert store.load()[1][0].unread


def test_failed_backup_integrity_check_prevents_apply(
    tmp_path, continuity_cache, monkeypatch, capsys
):
    path, store, arguments = continuity_cache
    backup = tmp_path / 'rejected-backup.db'
    before = database_rows(path)
    spec = importlib.util.spec_from_file_location('read_continuity_cli', SCRIPT)
    cli = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(cli)
    connect = sqlite3.connect

    class FailedIntegrityBackup(sqlite3.Connection):
        def execute(self, sql, *args, **kwargs):
            if sql == 'PRAGMA integrity_check':
                return super().execute("SELECT 'Synthetic corrupt backup'")
            return super().execute(sql, *args, **kwargs)

    def checked_connect(database, *args, **kwargs):
        if database == backup:
            kwargs['factory'] = FailedIntegrityBackup
        return connect(database, *args, **kwargs)

    monkeypatch.setattr(cli.sqlite3, 'connect', checked_connect)
    monkeypatch.setattr(sys, 'argv', [str(SCRIPT), *arguments, '--apply', '--backup', str(backup)])

    with pytest.raises(SystemExit) as failure:
        cli.main()

    assert failure.value.code == 1
    assert 'backup failed its integrity check' in capsys.readouterr().err
    assert database_rows(path) == before
    assert store.load()[1][0].unread
