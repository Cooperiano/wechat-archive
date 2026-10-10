"""Local deterministic context index over the private normalized archive."""
from __future__ import annotations

from contextlib import closing
from datetime import datetime
import json
import os
from pathlib import Path
import sqlite3
import stat
import tempfile
from zoneinfo import ZoneInfo

from .platform_runtime import CollectorLock
from .storage import private_directory

_COLUMNS = ('message_key', 'room_id', 'sender_id', 'sender_name', 'create_time',
            'local_type', 'content', 'parse_state', 'source_db', 'source_table',
            'local_id', 'server_id')
_SELECT = ','.join(_COLUMNS)
_SCHEMA = '''
CREATE TABLE IF NOT EXISTS state(key TEXT PRIMARY KEY,value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS context_messages(
 message_key TEXT PRIMARY KEY,room_id TEXT NOT NULL,local_day TEXT NOT NULL,
 sender_id TEXT,sender_name TEXT,create_time INTEGER,local_type INTEGER,
 content TEXT,parse_state TEXT,source_db TEXT,source_table TEXT,
 local_id INTEGER,server_id TEXT);
CREATE INDEX IF NOT EXISTS context_room_day_time
 ON context_messages(room_id,local_day,create_time,message_key);
CREATE TABLE IF NOT EXISTS context_days(
 room_id TEXT NOT NULL,local_day TEXT NOT NULL,message_count INTEGER NOT NULL,
 first_time INTEGER,last_time INTEGER,PRIMARY KEY(room_id,local_day));
'''


def _ro(path: Path) -> sqlite3.Connection:
    from .storage import safe_sqlite_files
    safe_sqlite_files(path)
    db = sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)
    db.row_factory = sqlite3.Row
    db.execute('PRAGMA query_only=ON')
    return db


def _state(db, key, default=None):
    row = db.execute('SELECT value FROM state WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


def _set(db, key, value):
    db.execute('INSERT INTO state VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               (key, str(value)))


def _source_id(source, path):
    tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    if 'archive_meta' in tables:
        row = source.execute("SELECT value FROM archive_meta WHERE key='source_id'").fetchone()
        if row and row[0]:
            value = row[0]
            try:
                parsed = json.loads(value)
                if isinstance(parsed, str):
                    return parsed
            except (ValueError, TypeError):
                pass
            return value
    metadata = path.with_name('source-info.json')
    if metadata.exists():
        value = json.loads(metadata.read_text(encoding='utf-8')).get('source_id')
        if value:
            return value
    raise ValueError('archive_source_identity_missing')


def _version(path):
    info = path.stat()
    wal = path.with_name(path.name + '-wal')
    wal_info = wal.stat() if wal.exists() else None
    wal_version = f'{wal_info.st_ino}:{wal_info.st_size}:{wal_info.st_mtime_ns}' if wal_info else 'none'
    return f'{info.st_dev}:{info.st_ino}:{info.st_size}:{info.st_mtime_ns}:{wal_version}'


def _day(timestamp):
    return datetime.fromtimestamp(int(timestamp), ZoneInfo('Asia/Shanghai')).date().isoformat()


def _refresh_day(db, room, day):
    db.execute('''INSERT INTO context_days(room_id,local_day,message_count,first_time,last_time)
      SELECT room_id,local_day,count(*),min(create_time),max(create_time)
      FROM context_messages WHERE room_id=? AND local_day=? GROUP BY room_id,local_day
      ON CONFLICT(room_id,local_day) DO UPDATE SET
       message_count=excluded.message_count,first_time=excluded.first_time,last_time=excluded.last_time''',
       (room, day))
    db.execute('''DELETE FROM context_days WHERE room_id=? AND local_day=?
      AND NOT EXISTS(SELECT 1 FROM context_messages WHERE room_id=? AND local_day=?)''',
      (room, day, room, day))


def _delete(db, key, fts, dirty):
    previous = db.execute('SELECT rowid,room_id,local_day FROM context_messages WHERE message_key=?', (key,)).fetchone()
    db.execute('DELETE FROM context_messages WHERE message_key=?', (key,))
    if fts:
        if previous:
            db.execute('DELETE FROM context_fts WHERE rowid=?', (previous[0],))
    if previous:
        dirty.add((previous[1], previous[2]))
        return 1
    return 0


def _upsert(db, row, fts, dirty):
    key = row['message_key']
    previous = db.execute('SELECT rowid,room_id,local_day FROM context_messages WHERE message_key=?', (key,)).fetchone()
    day = _day(row['create_time'])
    values = (key, row['room_id'], day, *(row[name] for name in _COLUMNS[2:]))
    db.execute('''INSERT INTO context_messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
      ON CONFLICT(message_key) DO UPDATE SET
      room_id=excluded.room_id,local_day=excluded.local_day,sender_id=excluded.sender_id,
      sender_name=excluded.sender_name,create_time=excluded.create_time,
      local_type=excluded.local_type,content=excluded.content,parse_state=excluded.parse_state,
      source_db=excluded.source_db,source_table=excluded.source_table,
      local_id=excluded.local_id,server_id=excluded.server_id''', values)
    if fts:
        if previous:
            db.execute('DELETE FROM context_fts WHERE rowid=?', (previous[0],))
        indexed_rowid = db.execute('SELECT rowid FROM context_messages WHERE message_key=?', (key,)).fetchone()[0]
        db.execute('INSERT INTO context_fts(rowid,message_key,content) VALUES(?,?,?)',
                   (indexed_rowid, key, row['content'] or ''))
    if previous and (previous[1], previous[2]) != (row['room_id'], day):
        dirty.add((previous[1], previous[2]))
    dirty.add((row['room_id'], day))
    return int(previous is None)


def _init(db):
    for statement in _SCHEMA.split(';'):
        if statement.strip():
            db.execute(statement)
    try:
        db.execute('CREATE VIRTUAL TABLE IF NOT EXISTS context_fts USING fts5(message_key UNINDEXED,content)')
        return True
    except sqlite3.OperationalError as exc:
        if 'fts5' not in str(exc).lower() and 'no such module' not in str(exc).lower():
            raise
        return False


def _reset(db, fts):
    db.execute('DELETE FROM context_messages')
    db.execute('DELETE FROM context_days')
    if fts:
        db.execute('DELETE FROM context_fts')
    db.execute('DELETE FROM state')


def _existing_target(path, identity):
    if not path.exists():
        if path.is_symlink():
            raise ValueError('unsafe_context_file')
        return
    info = path.lstat()
    if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
            (os.name == 'posix' and info.st_uid != os.getuid())):
        raise ValueError('unsafe_context_file')
    if info.st_size == 0:
        return
    with closing(_ro(path)) as existing:
        tables = {row[0] for row in existing.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if not {'state', 'context_messages', 'context_days'}.issubset(tables):
            raise ValueError('invalid_context_schema')
        columns = {row[1] for row in existing.execute('PRAGMA table_info(context_messages)')}
        if not {'message_key', 'room_id', 'local_day', 'content'}.issubset(columns):
            raise ValueError('invalid_context_schema')
        bound = existing.execute("SELECT value FROM state WHERE key='source_id'").fetchone()
        if not bound or not bound[0]:
            raise ValueError('invalid_context_binding')
        if bound[0] != identity:
            raise ValueError('context_bound_to_another_source')


def _safe_sidecars(path):
    for suffix in ('-wal', '-shm', '-journal'):
        sidecar = path.with_name(path.name + suffix)
        if not sidecar.exists() and not sidecar.is_symlink():
            continue
        info = sidecar.lstat()
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                (os.name == 'posix' and info.st_uid != os.getuid())):
            raise ValueError('unsafe_context_sidecar')


def _bootstrap_target(path, source, archive_path):
    """Publish a fully bound initial DB, leaving no partial target on failure."""
    descriptor, temporary_name = tempfile.mkstemp(prefix='.context-init-', suffix='.sqlite', dir=path.parent)
    os.close(descriptor)
    temporary = Path(temporary_name)
    try:
        with closing(sqlite3.connect(temporary)) as db:
            with db:
                db.execute('BEGIN IMMEDIATE')
                fts = _init(db)
                identity = _source_id(source, archive_path)
                _set(db, 'source_id', identity)
                _set(db, 'fts_ready', int(fts))
                if fts:
                    _set(db, 'fts_format', 'rowid-v1')
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def process_archive(archive_path, output, *, batch_size=1000):
    """Resume a bound archive index. Return only counts and fixed status labels."""
    if not isinstance(batch_size, int) or not 1 <= batch_size <= 10000:
        raise ValueError('invalid_batch_size')
    provided = Path(archive_path).expanduser().absolute()
    if provided.is_symlink():
        raise ValueError('archive_symlink_rejected')
    path = provided.resolve(strict=True)
    if not path.is_file():
        raise ValueError('archive_not_file')
    proposed = Path(output).expanduser().absolute()
    folder_path = proposed.resolve()
    if path.is_relative_to(folder_path):
        raise ValueError('source_output_overlap')
    target_path = folder_path / 'context.sqlite'
    if target_path.is_symlink() or (target_path.exists() and os.path.samefile(path, target_path)):
        raise ValueError('archive_context_alias')
    # Reject mismatched bindings before even creating/chmodding the directory.
    with closing(_ro(path)) as pre_source:
        identity = _source_id(pre_source, path)
    _safe_sidecars(target_path)
    _existing_target(target_path, identity)
    folder = private_directory(proposed)
    counts = {'indexed': 0, 'updated': 0, 'deleted': 0}
    with CollectorLock(folder / '.context.lock'):
        _safe_sidecars(target_path)
        _existing_target(target_path, identity)
        if not target_path.exists() or target_path.stat().st_size == 0:
            with closing(_ro(path)) as bootstrap_source:
                source_tables = {row[0] for row in bootstrap_source.execute(
                    "SELECT name FROM sqlite_master WHERE type='table'")}
                if not {'messages', 'groups'}.issubset(source_tables):
                    raise ValueError('unsupported_archive_schema')
                _bootstrap_target(target_path, bootstrap_source, path)
        with closing(_ro(path)) as source, closing(sqlite3.connect(target_path)) as target:
            target.execute('PRAGMA journal_mode=WAL')
            fts = _init(target)
            with target:
                if not fts:
                    _set(target, 'fts_ready', 0)
                elif _state(target, 'fts_format') != 'rowid-v1':
                    existing_rows = target.execute('SELECT count(*) FROM context_messages').fetchone()[0]
                    _set(target, 'fts_ready', int(existing_rows == 0))
                    if existing_rows == 0:
                        _set(target, 'fts_format', 'rowid-v1')
            identity = _source_id(source, path)
            bound = _state(target, 'source_id')
            if bound and bound != identity:
                raise ValueError('context_bound_to_another_source')
            tables = {row[0] for row in source.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            if not {'messages', 'groups'}.issubset(tables):
                raise ValueError('unsupported_archive_schema')
            feed = 'change_feed' in tables
            generation_row = (source.execute("SELECT value FROM archive_meta WHERE key='generation'").fetchone()
                              if 'archive_meta' in tables else None)
            generation = generation_row[0] if generation_row else None
            version = generation if feed and generation else _version(path)
            prior_version = _state(target, 'archive_version')
            # Generation changes require a full reconciliation. Old archives
            # without a feed also reconcile whenever their file changes.
            if prior_version and prior_version != version:
                with target:
                    _reset(target, fts)
            with target:
                _set(target, 'source_id', identity)
                _set(target, 'archive_version', version)
                if _state(target, 'baseline_cursor') is None:
                    _set(target, 'baseline_cursor', 0)
                    _set(target, 'baseline_done', 0)
                    bookmark = source.execute('SELECT coalesce(max(seq),0) FROM change_feed').fetchone()[0] if feed else 0
                    _set(target, 'feed_bookmark', bookmark)
                    _set(target, 'feed_cursor', bookmark)
            # Source read transaction pins a consistent baseline. The bookmark
            # precedes it, so every later feed change is replayed afterwards.
            source.execute('BEGIN')
            work_done = False
            try:
                if _state(target, 'baseline_done') != '1':
                    cursor = int(_state(target, 'baseline_cursor', '0'))
                    rows = source.execute(f'SELECT rowid,{_SELECT} FROM messages WHERE rowid>? ORDER BY rowid LIMIT ?',
                                          (cursor, batch_size)).fetchall()
                    with target:
                        dirty = set()
                        for row in rows:
                            counts['indexed'] += _upsert(target, row, fts, dirty)
                        for room, day in dirty:
                            _refresh_day(target, room, day)
                        if rows:
                            _set(target, 'baseline_cursor', rows[-1]['rowid'])
                            work_done = True
                        else:
                            _set(target, 'baseline_done', 1)
            finally:
                source.execute('ROLLBACK')
            if (fts and _state(target, 'baseline_done') == '1' and
                    _state(target, 'fts_ready') != '1' and not work_done):
                reindex_cursor = _state(target, 'fts_reindex_cursor')
                with target:
                    if reindex_cursor is None:
                        target.execute('DELETE FROM context_fts')
                        _set(target, 'fts_reindex_cursor', 0)
                        reindex_cursor = 0
                    rows = target.execute('''SELECT rowid,message_key,content FROM context_messages
                      WHERE rowid>? ORDER BY rowid LIMIT ?''',
                      (reindex_cursor, batch_size)).fetchall()
                    for row in rows:
                        target.execute('INSERT INTO context_fts(rowid,message_key,content) VALUES(?,?,?)',
                                       (row[0], row[1], row[2] or ''))
                    if rows:
                        _set(target, 'fts_reindex_cursor', rows[-1][0])
                    else:
                        _set(target, 'fts_ready', 1)
                        _set(target, 'fts_format', 'rowid-v1')
                        target.execute("DELETE FROM state WHERE key='fts_reindex_cursor'")
                    work_done = True
            if feed:
                source.execute('BEGIN')
                try:
                    # A bounded high-water mark leaves concurrent arrivals for
                    # the next run; cursor and derived changes commit together.
                    ceiling = source.execute('SELECT coalesce(max(seq),0) FROM change_feed').fetchone()[0]
                    cursor = int(_state(target, 'feed_cursor', '0'))
                    if ceiling < cursor:
                        raise ValueError('change_feed_history_lost')
                    if _state(target, 'baseline_done') == '1' and not work_done and cursor < ceiling:
                        entries = source.execute('''SELECT seq,message_key,operation FROM change_feed
                          WHERE seq>? AND seq<=? ORDER BY seq LIMIT ?''',
                          (cursor, ceiling, batch_size)).fetchall()
                        if not entries:
                            raise ValueError('change_feed_history_lost')
                        with target:
                            dirty = set()
                            for entry in entries:
                                if entry['seq'] != cursor + 1:
                                    raise ValueError('change_feed_history_lost')
                                if entry['operation'] not in ('upsert', 'delete'):
                                    raise ValueError('unknown_change_operation')
                                row = source.execute(f'SELECT {_SELECT} FROM messages WHERE message_key=?',
                                                     (entry['message_key'],)).fetchone()
                                if entry['operation'] == 'delete' or row is None:
                                    counts['deleted'] += _delete(target, entry['message_key'], fts, dirty)
                                else:
                                    added = _upsert(target, row, fts, dirty)
                                    counts['indexed'] += added
                                    counts['updated'] += 1 - added
                                cursor = entry['seq']
                            for room, day in dirty:
                                _refresh_day(target, room, day)
                            _set(target, 'feed_cursor', cursor)
                finally:
                    source.execute('ROLLBACK')
            baseline_complete = _state(target, 'baseline_done') == '1'
            if baseline_complete:
                with closing(_ro(path)) as status_source:
                    feed_ceiling = (status_source.execute('SELECT coalesce(max(seq),0) FROM change_feed').fetchone()[0]
                                    if feed else 0)
                pending = int(feed_ceiling > int(_state(target, 'feed_cursor', '0')) or
                              (fts and _state(target, 'fts_ready') != '1'))
            else:
                pending = 1
            state = 'pending' if pending else 'complete' if fts else 'index-degraded'
            processed = sum(counts.values())
            return {**counts, 'processed': processed, 'pending': pending,
                    'indexed_messages': target.execute('SELECT count(*) FROM context_messages').fetchone()[0],
                    'baseline_complete': baseline_complete,
                    'feed_cursor': int(_state(target, 'feed_cursor', '0')),
                    'conversations': target.execute('SELECT count(DISTINCT room_id) FROM context_messages').fetchone()[0],
                    'days': target.execute('SELECT count(*) FROM context_days').fetchone()[0], 'state': state}


def search_context(output, query, *, limit=50):
    """Literal phrase search with bound FTS input; results stay in private caller."""
    if not isinstance(query, str) or not query.strip() or not isinstance(limit, int) or not 1 <= limit <= 100:
        raise ValueError('invalid_search')
    path = Path(output).expanduser().resolve() / 'context.sqlite'
    with closing(_ro(path)) as db:
        if (_state(db, 'fts_ready') != '1' or
                not db.execute("SELECT 1 FROM sqlite_master WHERE name='context_fts'").fetchone()):
            return []
        literal = '"' + query.replace('"', '""') + '"'
        rows = db.execute('''SELECT m.message_key,m.room_id,m.local_day,m.create_time,m.sender_id,
          m.sender_name,m.content,m.parse_state,m.source_db,m.source_table,m.local_id,m.server_id
          FROM context_fts f JOIN context_messages m ON m.rowid=f.rowid
          WHERE context_fts MATCH ? ORDER BY m.create_time,m.message_key LIMIT ?''', (literal, limit)).fetchall()
        return [dict(row) for row in rows]
