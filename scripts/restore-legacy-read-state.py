"""Preview or explicitly bind legacy update triage to its confirmed owner."""
from __future__ import annotations

import argparse
import json
import sqlite3
import sys
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from config import load_settings
from store import Store


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--config', type=Path, required=True)
    parser.add_argument('--database', type=Path, required=True)
    parser.add_argument('--connector', choices=('jira', 'azure_devops'), required=True)
    parser.add_argument('--scope', required=True, help='Verified account scope from the current cache')
    parser.add_argument('--apply', action='store_true', help='Confirm legacy ownership and apply the preview')
    parser.add_argument('--backup', type=Path, help='New SQLite backup file; required with --apply')
    args = parser.parse_args()
    if args.apply and args.backup is None:
        parser.error('--apply requires --backup')
    try:
        if not args.database.is_file():
            raise ValueError('The existing database must be specified')
        settings = load_settings(args.config)
        store = Store(args.database, settings.cache_bindings)
        counts = store.bind_legacy_read_state(args.connector, args.scope)
        if args.apply:
            # Use SQLite's backup API so a live WAL is included consistently.
            # Exclusive creation prevents replacing any existing backup.
            with args.backup.open('xb'):
                pass
            with sqlite3.connect(args.database.resolve().as_uri() + '?mode=ro', uri=True) as source:
                with sqlite3.connect(args.backup) as backup:
                    source.backup(backup)
                    if backup.execute('PRAGMA integrity_check').fetchall() != [('ok',)]:
                        raise ValueError('The pre-migration backup failed its integrity check')
            counts = store.bind_legacy_read_state(args.connector, args.scope, apply=True)
        print(json.dumps({'applied': args.apply, 'connector': args.connector, 'scope': args.scope,
                          'counts': counts, 'backup': str(args.backup) if args.apply else None}, indent=2))
    except (OSError, sqlite3.Error, ValueError) as error:
        parser.exit(1, f'Read-state continuity failed: {error}\n')


if __name__ == '__main__':
    main()
