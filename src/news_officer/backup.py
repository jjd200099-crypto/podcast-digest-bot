"""Create a private, verified snapshot; never overwrite/restore production."""

import argparse
import hashlib
import json
import shutil
import sqlite3
from datetime import UTC, datetime
from pathlib import Path

from .file_memory import PodcastFileMemory
from .store import Store


def file_hash(path):
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b''):
            digest.update(chunk)
    return digest.hexdigest()


def create_backup(database: Path, memory: Path, destination: Path) -> dict:
    database, memory, destination = (p.absolute() for p in (database, memory, destination))
    if destination.exists() or destination.is_symlink():
        raise ValueError('Backup destination must be a new dedicated directory')
    if memory.is_symlink() or database.is_symlink():
        raise ValueError('Backup sources cannot be symlinks')
    database, memory, destination = (p.resolve() for p in (database, memory, destination))
    if destination.is_relative_to(memory) or memory.is_relative_to(destination):
        raise ValueError('Backup must be outside the live memory tree')
    destination.mkdir(parents=True, mode=0o700)
    destination.chmod(0o700)
    snapshot = destination / 'state.sqlite3'
    with (sqlite3.connect(database.resolve().as_uri() + '?mode=ro', uri=True) as source,
          sqlite3.connect(snapshot) as target):
        source.backup(target)
        if target.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Backup database failed integrity check')
    snapshot.chmod(0o600)
    copied_memory = destination / 'podcast-memory'
    copied_memory.mkdir(mode=0o700)
    # Retain old immutable versions too. Rebuild the reading index against the
    # SQLite snapshot so live concurrent ingestion cannot make it inconsistent.
    if memory.exists():
        for path in memory.rglob('*'):
            if path.is_symlink():
                raise ValueError('Memory tree contains a symlink')
            if not path.is_file() or path.name.startswith('.pending-'):
                continue
            target = copied_memory / path.relative_to(memory)
            target.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
            shutil.copyfile(path, target)
            target.chmod(0o600)
    archive = PodcastFileMemory(Store(snapshot), copied_memory)
    archive.sync()
    if archive.warnings:
        raise ValueError('Backup archive failed source integrity verification')
    files = {str(p.relative_to(destination)): file_hash(p) for p in destination.rglob('*') if p.is_file()}
    manifest = {'format': 'news-officer-backup-v1', 'created_at': datetime.now(UTC).isoformat(), 'files': files}
    manifest_path = destination / 'manifest.json'
    manifest_path.write_text(json.dumps(manifest, sort_keys=True, ensure_ascii=False, indent=2))
    manifest_path.chmod(0o600)
    verify_backup(destination)
    return {'verified': True, 'files': len(files), 'destination': str(destination), 'offsite': False}


def verify_backup(destination: Path) -> dict:
    manifest = json.loads((destination / 'manifest.json').read_text())
    if manifest.get('format') != 'news-officer-backup-v1':
        raise ValueError('Unknown backup format')
    for relative, expected in manifest['files'].items():
        path = destination / relative
        if (path.is_symlink() or not path.resolve().is_relative_to(destination.resolve())
                or file_hash(path) != expected):
            raise ValueError('Backup file hash mismatch or unsafe path')
    with sqlite3.connect((destination / 'state.sqlite3').resolve().as_uri() + '?mode=ro', uri=True) as db:
        if db.execute('PRAGMA integrity_check').fetchone()[0] != 'ok':
            raise ValueError('Backup database failed integrity check')
    return {'verified': True, 'files': len(manifest['files'])}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path)
    parser.add_argument('--memory', type=Path)
    parser.add_argument('--output', type=Path, required=True)
    parser.add_argument('--verify-only', action='store_true')
    args = parser.parse_args()
    if not args.verify_only and (not args.db or not args.memory):
        parser.error('--db and --memory are required for a new snapshot')
    result = (verify_backup(args.output) if args.verify_only
              else create_backup(args.db, args.memory, args.output))
    print(json.dumps(result))


if __name__ == '__main__':
    main()
