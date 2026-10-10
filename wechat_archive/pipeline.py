"""Explicit local import, validation and authenticated snapshot generations."""
from contextlib import closing
import hashlib
import json
import os
from pathlib import Path
import sqlite3
import uuid
from .cipher import load_cipher, verify_enc_key
from .parser import atomic_json, now, snapshot_extension_index, parse_snapshots
from .snapshot import snapshot_database, SnapshotError
from .storage import private_directory, relative_database, separated


def databases(source):
    root = Path(source).expanduser().resolve()
    found = []
    def denied(error):
        raise error
    for directory, folders, files in os.walk(root, followlinks=False, onerror=denied):
        if any((Path(directory) / name).is_symlink() for name in folders):
            raise ValueError('source_contains_symlink')
        for name in sorted(files):
            if name.endswith('.db'):
                path = relative_database(root, (Path(directory) / name).relative_to(root).as_posix())
                with path.open('rb') as handle:
                    page = handle.read(4096)
                if len(page) != 4096 or page.startswith(b'SQLite format 3\x00'):
                    raise ValueError('unsupported_source_database')
                found.append((path.relative_to(root).as_posix(), path, page))
    if not found:
        raise ValueError('no_source_databases')
    return sorted(found)


def read_key_json(path):
    path = Path(path).expanduser()
    if path.is_symlink() or path.stat().st_size > 2 * 1024 * 1024:
        raise ValueError('invalid_key_input')
    if os.name == 'posix' and (path.stat().st_uid != os.getuid() or path.stat().st_mode & 0o077):
        raise ValueError('key_input_permissions_unsafe')
    return json.loads(path.read_text())


def validated_keys(records, payload):
    by_path = payload.get('keys', {})
    by_salt = payload.get('keys_by_salt', {})
    raw = payload.get('raw_key')
    result = {}
    for name, _, page in records:
        item = by_path.get(name, by_salt.get(page[:16].hex()))
        if isinstance(item, dict):
            item = item.get('enc_key')
        try:
            key = bytes.fromhex(item) if item else hashlib.pbkdf2_hmac('sha512', bytes.fromhex(raw), page[:16], 256000, 32) if raw else b''
        except (ValueError, TypeError):
            raise ValueError('invalid_key_input') from None
        if not verify_enc_key(key, page):
            raise ValueError('database_key_coverage_incomplete')
        result[name] = key
    return result


def snapshot(source, output, payload):
    separated(source, output)
    output = private_directory(output)
    records = databases(source)
    keys = validated_keys(records, payload)
    source_id = hashlib.sha256(os.fsencode(Path(source).expanduser().resolve())).hexdigest()
    identity_file = output / 'source-info.json'
    if identity_file.exists() and json.loads(identity_file.read_text()).get('source_id') != source_id:
        raise ValueError('output_belongs_to_another_source')
    generation = private_directory(output / 'snapshots' / uuid.uuid4().hex)
    manifest = {'database_count': len(records), 'databases': [], 'started_at': now(),
                'source_id': source_id, 'snapshot_generation': generation.name,
                'consistency': 'per_database_double_read'}
    for name, path, _ in records:
        dst = relative_database(generation / 'databases', name)
        private_directory(dst.parent)
        entry = {'database': name}
        try:
            entry.update(state='ok', metrics=snapshot_database(path, dst, keys[name], cipher_module=load_cipher()))
        except SnapshotError:
            if not name.endswith('_fts.db'):
                raise
            entry.update(state='extension_required', metrics=snapshot_extension_index(path, dst, keys[name]))
        manifest['databases'].append(entry)
    manifest.update(verified_count=sum(d['state'] == 'ok' for d in manifest['databases']),
                    extension_required_count=sum(d['state'] == 'extension_required' for d in manifest['databases']),
                    authenticated_count=len(records), failed_count=0,
                    wal_committed_frames=sum(d['metrics']['committed_frames'] for d in manifest['databases']),
                    finished_at=now())
    atomic_json(generation / 'snapshot-status.json', manifest)
    stats = parse_snapshots(generation, output)
    if stats['state'] == 'partial':
        raise ValueError('unsupported_message_schema')
    return stats
