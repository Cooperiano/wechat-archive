"""Full local snapshot parser; never loads device state at import time."""
from __future__ import annotations
import base64
from contextlib import closing
from datetime import datetime
from zoneinfo import ZoneInfo
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import shutil
import sys
import tempfile
from .cipher import load_cipher
from .codec import decode_text
from .snapshot import _capture_source, parse_wal, SQLCipherPageReader, _write_snapshot
from .vendor import appmsg, system_messages
from .storage import relative_database, separated

def now():
    return datetime.now(ZoneInfo('Asia/Shanghai')).isoformat(timespec='seconds')


def atomic_json(path, value):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix='.write-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            json.dump(value, f, ensure_ascii=False, indent=2)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def atomic_text(path, value):
    path = Path(path)
    fd, tmp = tempfile.mkstemp(prefix='.page-', dir=path.parent)
    try:
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            f.write(value)
        os.replace(tmp, path)
    finally:
        Path(tmp).unlink(missing_ok=True)


def publish_archive(output, database, metadata):
    """Stage all data before commit; roll back the entire set on write failure."""
    staging = Path(tempfile.mkdtemp(prefix='.publish-', dir=output))
    retain = False
    try:
        names = [*metadata, 'archive.sqlite']
        for name, value in metadata.items():
            atomic_json(staging / name, value)
        os.replace(database, staging / 'archive.sqlite')
        backup = staging / 'previous'
        backup.mkdir()
        existing = set()
        for name in names:
            if (output / name).exists():
                try:
                    os.link(output / name, backup / name)
                except OSError:
                    shutil.copy2(output / name, backup / name)
                existing.add(name)
        committed = []
        try:
            for name in names:
                os.replace(staging / name, output / name)
                committed.append(name)
        except BaseException:
            failures = []
            for name in reversed(committed):
                try:
                    if name in existing:
                        os.replace(backup / name, output / name)
                    else:
                        (output / name).unlink(missing_ok=True)
                except BaseException:
                    failures.append(name)
            if failures:
                retain = True
                raise ValueError('archive_recovery_required') from None
            raise
    finally:
        if not retain:
            shutil.rmtree(staging)


def ro(path):
    conn = sqlite3.connect(Path(path).resolve().as_uri() + '?mode=ro&immutable=1', uri=True)
    conn.row_factory = sqlite3.Row
    conn.execute('PRAGMA query_only=ON')
    return conn


def ident(value):
    return '"' + value.replace('"', '""') + '"'


def tables(db):
    return [r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table' ORDER BY name")]


def columns(db, table):
    return [r[1] for r in db.execute('PRAGMA table_info(' + ident(table) + ')')]


def snapshot_extension_index(source, destination, key):
    """Retain authenticated FTS data even when WCDB's tokenizer is unavailable.

    This is NOT a passed SQLite quick_check. Every encrypted page and WAL frame
    is still authenticated by the existing snapshot implementation. Only a
    specific missing-module/tokenizer failure is eligible for this fallback.
    """
    source, destination = Path(source).expanduser().resolve(), Path(destination).expanduser().resolve()
    protected = {source, *(Path(str(source) + suffix) for suffix in ('-wal', '-shm', '-journal'))}
    if destination in protected or (destination.exists() and any(item.exists() and os.path.samefile(destination, item) for item in protected)):
        raise ValueError('snapshot_output_aliases_source')
    database, wal_bytes = _capture_source(source)
    if not database or len(database) % 4096:
        raise RuntimeError('Database size is not a whole number of pages')
    wal = parse_wal(wal_bytes)
    reader = SQLCipherPageReader(key, database[:4096], load_cipher())
    fd, filename = tempfile.mkstemp(prefix='.fts-', suffix='.db', dir=destination.parent)
    os.close(fd)
    temporary = Path(filename)
    try:
        metrics = _write_snapshot(temporary, database, wal, reader)
        with closing(ro(temporary)) as db:
            schemas = [r[0] or '' for r in db.execute("SELECT sql FROM sqlite_master WHERE type='table'")]
            # WCDB's MMFtsTokenizer is not registered with general SQLite.
            # Confirm this on a synthetic in-memory table, not the source DB.
            needs_mm = any(re.search(r'\bMMFtsTokenizer\b', sql) for sql in schemas)
            missing_mm = False
            if needs_mm:
                with closing(sqlite3.connect(':memory:')) as probe:
                    try:
                        probe.execute("CREATE VIRTUAL TABLE probe USING fts5(content, tokenize='MMFtsTokenizer')")
                    except sqlite3.OperationalError as error:
                        missing_mm = 'no such tokenizer' in str(error).lower()
            try:
                checks = db.execute('PRAGMA quick_check').fetchall()
            except sqlite3.OperationalError as exc:
                error = str(exc).lower()
                if ('no such tokenizer' not in error and 'no such module' not in error
                        and not (missing_mm and error == 'sql logic error')):
                    raise
            else:
                if [tuple(r) for r in checks] != [('ok',)]:
                    raise RuntimeError('Index failed SQLite validation')
                # The ordinary path should already have succeeded in this case.
                raise RuntimeError('Missing SQLite extension was not confirmed')
        metrics.update(db_pages=len(database) // 4096, wal_frames=len(wal.frames),
                       committed_frames=wal.committed_frames, quick_check='requires_extension',
                       encrypted_pages_authenticated=True, source_consistency='double_read')
        os.replace(temporary, destination)
        return metrics
    finally:
        temporary.unlink(missing_ok=True)


def raw_json(row):
    return json.dumps({k: {'encoding': 'base64', 'data': base64.b64encode(v).decode('ascii')}
                       if isinstance(v, bytes) else v for k, v in dict(row).items()}, ensure_ascii=False)


SCHEMA = '''
CREATE TABLE groups(room_id TEXT PRIMARY KEY,name TEXT,kind TEXT,source_rows INTEGER DEFAULT 0);
CREATE TABLE messages(message_key TEXT PRIMARY KEY,room_id TEXT,sender_id TEXT,sender_name TEXT,
 create_time INTEGER,local_type INTEGER,content TEXT,parse_state TEXT,source_db TEXT,source_table TEXT,
 local_id INTEGER,server_id TEXT,raw_json TEXT,archived_at TEXT);
CREATE INDEX messages_room_time ON messages(room_id,create_time);
CREATE TABLE source_records(source_db TEXT,source_table TEXT,local_id INTEGER,message_key TEXT,
 raw_json TEXT,PRIMARY KEY(source_db,source_table,local_id));
'''


def decode(row, sender, kind):
    base_type = int(row['local_type']) & 0xffffffff
    value = row['message_content']
    compressed = row.get('compress_content')
    if isinstance(compressed, (str, bytes)) and compressed:
        value = compressed
    text, state = decode_text(value)
    if kind == 'group' and sender and text.startswith(sender + ':\n'):
        text = text[len(sender) + 2:]
    if 'unparsed' in state:
        return text, state, base_type
    if base_type == 49 and text:
        parsed = appmsg.parse_app_message(text, row['local_type'])
        return '[' + parsed['label'] + '] ' + parsed['summary'], state, base_type
    if base_type in (10000, 10002) and text:
        parsed = system_messages.parse_system_message(text, 'recall' if base_type == 10002 else '')
        return parsed['text'], state, base_type
    if base_type != 1:
        labels = {'3': '图片', '34': '语音', '42': '名片', '43': '视频', '47': '表情', '48': '位置', '50': '通话'}
        # Preserve record and metadata without treating its binary/XML body as a playable attachment.
        label = labels.get(str(base_type), '类型 ' + str(base_type))
        return '[' + label + '：记录已保留，媒体本体未在此页解码]', state, base_type
    return text, state, base_type


def parse_snapshots(snapshots, output):
    OUT = Path(output).expanduser().resolve()
    snapshots = Path(snapshots).expanduser().resolve()
    if OUT.resolve().is_relative_to(snapshots.resolve()):
        raise ValueError('snapshot_output_overlap')
    manifest = json.loads((snapshots / 'snapshot-status.json').read_text())
    source_id = manifest.get('source_id') or hashlib.sha256(os.fsencode(snapshots.resolve())).hexdigest()
    identity_file = OUT / 'source-info.json'
    if identity_file.exists():
        if json.loads(identity_file.read_text()).get('source_id') != source_id:
            raise ValueError('output_belongs_to_another_source')
    elif (OUT / 'archive.sqlite').exists():
        raise ValueError('existing_archive_without_source_binding')
    # Only this run's authenticated snapshots are eligible, never stale failed files.
    if manifest.get('failed_count') or not manifest.get('databases'):
        raise ValueError('incomplete_snapshot_manifest')
    paths = [(d['database'], relative_database(snapshots / 'databases', d['database'])) for d in manifest['databases'] if d['state'] in ('ok', 'extension_required')]
    if len(paths) != manifest['database_count'] or len(set(name for name, _ in paths)) != len(paths):
        raise ValueError('incomplete_snapshot_manifest')
    OUT.mkdir(parents=True, exist_ok=True, mode=0o700)
    names, candidates, contacts = {}, set(), []
    for rel, path in paths:
        with closing(ro(path)) as db:
            ts = tables(db)
            if rel == 'contact/contact.db' and 'contact' in ts:
                for row in db.execute('SELECT * FROM contact'):
                    user = row['username']
                    if not user:
                        continue
                    candidates.add(user)
                    names[user] = row['remark'] or row['nick_name'] or user
                    contacts.append({'username': user, 'name': names[user]})
            if 'Name2Id' in ts and 'user_name' in columns(db, 'Name2Id'):
                candidates.update(r[0] for r in db.execute('SELECT user_name FROM Name2Id') if r[0])
            if rel == 'session/session.db' and 'SessionTable' in ts and 'username' in columns(db, 'SessionTable'):
                candidates.update(r[0] for r in db.execute('SELECT username FROM SessionTable') if r[0])
    mapping = {'Msg_' + hashlib.md5(user.encode()).hexdigest(): user for user in candidates}
    fd, temp_name = tempfile.mkstemp(prefix='.archive-', suffix='.sqlite', dir=OUT)
    os.close(fd)
    temp_path = Path(temp_name)
    source_rows = 0
    catalog, diagnostics = [], []
    captured = now()
    try:
        with closing(sqlite3.connect(temp_path)) as target:
            target.executescript(SCHEMA)
            for rel, path in paths:
                db_rows = 0
                with closing(ro(path)) as db:
                    ts = tables(db)
                    sender_map = ({r[0]: r[1] for r in db.execute('SELECT rowid,user_name FROM Name2Id')}
                                  if 'Name2Id' in ts and 'user_name' in columns(db, 'Name2Id') else {})
                    inventory = {'database': rel, 'tables': []}
                    for table in ts:
                        try:
                            cols = columns(db, table)
                            count = db.execute('SELECT count(*) FROM ' + ident(table)).fetchone()[0]
                            inventory['tables'].append({'name': table, 'columns': cols, 'row_count': count})
                        except sqlite3.Error:
                            cols = []
                            inventory['tables'].append({'name': table, 'columns': cols, 'row_count': None,
                                                        'state': 'requires SQLite extension'})
                        if not table.startswith('Msg_'):
                            continue
                        needed = {'local_id', 'server_id', 'local_type', 'create_time', 'real_sender_id', 'message_content'}
                        if not needed.issubset(cols):
                            diagnostics.append({'database': rel, 'table': table, 'state': 'unsupported_message_schema'})
                            continue
                        room = mapping.get(table, table)
                        kind = ('unmapped' if room == table else 'group' if room.endswith('@chatroom') else
                                'service' if room.startswith('gh_') else 'system' if room in
                                {'filehelper', 'weixin', 'fmessage', 'newsapp', 'mphelper', 'brandsessionholder'} else 'private')
                        name = names.get(room, '未映射会话 · ' + table[4:12] if kind == 'unmapped' else room)
                        target.execute('INSERT OR IGNORE INTO groups VALUES(?,?,?,0)', (room, name, kind))
                        for row in db.execute('SELECT * FROM ' + ident(table)):
                            data = dict(row)
                            local_id = int(data['local_id'])
                            server = str(data['server_id'])
                            msg_key = (f'{room}:server:{server}' if server.isdecimal() and int(server) > 0 else
                                       f'{room}:{rel}:{table}:local:{local_id}')
                            raw = raw_json(row)
                            target.execute('INSERT INTO source_records VALUES(?,?,?,?,?)', (rel, table, local_id, msg_key, raw))
                            source_rows += 1
                            db_rows += 1
                            target.execute('UPDATE groups SET source_rows=source_rows+1 WHERE room_id=?', (room,))
                            prior = target.execute('SELECT source_db,local_id FROM messages WHERE message_key=?', (msg_key,)).fetchone()
                            if prior and (prior[0], prior[1]) >= (rel, local_id):
                                continue
                            sender = sender_map.get(data['real_sender_id'], '')
                            sender_name = names.get(sender, sender) if sender else '发送者未映射'
                            content, state, base_type = decode(data, sender, kind)
                            target.execute('INSERT OR REPLACE INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                                           (msg_key, room, sender, sender_name, data['create_time'], base_type,
                                            content, state, rel, table, local_id, server, raw, captured))
                    catalog.append(inventory)
                target.commit()
                # Keep private database/table identities out of the progress stream.
            check = target.execute('PRAGMA quick_check').fetchall()
            if check != [('ok',)]:
                raise RuntimeError('Normalized archive failed validation')
            canonical = target.execute('SELECT count(*),min(create_time),max(create_time) FROM messages').fetchone()
            kind_counts = dict(target.execute('SELECT kind,count(*) FROM groups GROUP BY kind'))
            parsed_counts = dict(target.execute('SELECT parse_state,count(*) FROM messages GROUP BY parse_state'))
            stats = {'updated_at': captured, 'platform': manifest.get('platform', sys.platform), 'wechat_version': manifest.get('wechat_version', 'unknown'),
                     'state': 'messages_complete_indexes_require_extension' if not manifest['failed_count'] and not diagnostics and manifest.get('extension_required_count') else
                              'snapshot_complete' if not manifest['failed_count'] and not diagnostics else 'partial',
                     'groups': kind_counts.get('group', 0), 'private_chats': kind_counts.get('private', 0),
                     'service_chats': kind_counts.get('service', 0), 'system_chats': kind_counts.get('system', 0),
                     'unmapped_chats': kind_counts.get('unmapped', 0), 'conversations': sum(kind_counts.values()),
                     'source_rows': source_rows, 'archived_messages': canonical[0],
                     'source_unique_messages': canonical[0], 'duplicate_source_rows': source_rows - canonical[0],
                     'missing_source_messages': target.execute('SELECT count(*) FROM source_records r LEFT JOIN messages m ON r.message_key=m.message_key WHERE m.message_key IS NULL').fetchone()[0],
                     'unparsed_messages': sum(n for state, n in parsed_counts.items() if 'unparsed' in state),
                     'parse_states': parsed_counts, 'oldest_timestamp': canonical[1], 'newest_timestamp': canonical[2],
                     'verified_snapshots': manifest['verified_count'], 'failed_snapshots': manifest['failed_count'],
                     'database_count': manifest['database_count'],
                     'authenticated_snapshots': manifest.get('authenticated_count', manifest['verified_count']),
                     'extension_required_snapshots': manifest.get('extension_required_count', 0),
                     'wal_committed_frames': manifest['wal_committed_frames'],
                     'catalog_database_count': len(catalog), 'catalog_table_count': sum(len(d['tables']) for d in catalog),
                     'contact_count': len(contacts), 'message_table_diagnostics': diagnostics,
                     'scope': 'all_local_message_tables_including_service_system_unmapped',
                     'limits': ['仅包含此设备已收到并保留的数据；不表示云端或其他设备全部历史',
                                '逐库一致快照，各库未同时冻结；后台持续采集未启用',
                                '媒体记录和元数据已保留，媒体本体不在此页面解码']}
        if stats['state'] == 'partial':
            raise ValueError('unsupported_message_schema')
        publish_archive(OUT, temp_path, {
            'database-catalog.json': catalog, 'contacts.json': contacts,
            'status.json': stats, 'source-info.json': {'source_id': source_id,
                'snapshot_generation': manifest.get('snapshot_generation')},
        })
    finally:
        temp_path.unlink(missing_ok=True)
    return stats
