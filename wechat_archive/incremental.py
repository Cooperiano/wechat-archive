"""Authenticated changed-database snapshots and transactional canonical deltas."""
from contextlib import closing
import base64
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import time
import uuid

from .cipher import load_cipher
from .parser import (parse_snapshots, snapshot_extension_index, ro, tables, columns,
                     ident, raw_json, decode, now, atomic_json)
from .pipeline import databases, validated_keys
from .platform_runtime import CollectorLock
from .snapshot import snapshot_database, SnapshotError
from .storage import private_directory, relative_database, separated, safe_sqlite_files


def _get(db, key, default=None):
    row = db.execute('SELECT value FROM archive_meta WHERE key=?', (key,)).fetchone()
    return row[0] if row else default


def _set(db, key, value):
    db.execute('INSERT INTO archive_meta VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value',
               (key, str(value)))


def fingerprint(path):
    """Cheap change hint; authentication still covers all pages on recapture."""
    result = []
    for file in (path, Path(str(path)+'-wal'), Path(str(path)+'-journal')):
        if file.is_symlink():
            raise ValueError('source_sidecar_symlink')
        try:
            before=file.stat()
            with file.open('rb') as reader:
                sample=reader.read(65536)
                if before.st_size>65536:
                    reader.seek(max(0,before.st_size-65536));sample+=reader.read(65536)
            after=file.stat()
            fields=lambda s:[s.st_size,s.st_mtime_ns,s.st_ctime_ns,s.st_ino]
            if fields(before)!=fields(after):raise ValueError('source_changed_during_hint')
            result.append([*fields(after),hashlib.sha256(sample).hexdigest()])
        except FileNotFoundError:
            result.append(None)
    return result


def _paths(output, entries):
    return {name:relative_database(output, entry['path']) for name,entry in entries.items()}


def _mapping(paths, db):
    names,candidates={},set(row[0] for row in db.execute('SELECT room_id FROM groups'))
    contact=paths.get('contact/contact.db')
    if contact:
        with closing(ro(contact)) as source:
            if 'contact' in tables(source):
                for row in source.execute('SELECT username,remark,nick_name FROM contact'):
                    if row['username']:
                        candidates.add(row['username']);names[row['username']]=row['remark'] or row['nick_name'] or row['username']
    senders={}
    for name,path in paths.items():
        with closing(ro(path)) as source:
            ts=tables(source)
            if 'Name2Id' in ts and 'user_name' in columns(source,'Name2Id'):
                mapping=dict(source.execute('SELECT rowid,user_name FROM Name2Id'))
                senders[name]=mapping;candidates.update(value for value in mapping.values() if value)
            if 'SessionTable' in ts and 'username' in columns(source,'SessionTable'):
                candidates.update(row[0] for row in source.execute('SELECT username FROM SessionTable') if row[0])
    mapping={'Msg_'+hashlib.md5(value.encode()).hexdigest():value for value in candidates if not value.startswith('Msg_')}
    return names,mapping,senders


def _data(raw):
    values=json.loads(raw)
    return {key:base64.b64decode(value['data']) if isinstance(value,dict) and value.get('encoding')=='base64' else value
            for key,value in values.items()}


def _room(table,mapping,names):
    room=mapping.get(table,table)
    kind=('unmapped' if room==table else 'group' if room.endswith('@chatroom') else 'service' if room.startswith('gh_')
          else 'system' if room in {'filehelper','weixin','fmessage','newsapp','mphelper','brandsessionholder'} else 'private')
    return room,names.get(room,'未映射会话 · '+table[4:12] if kind=='unmapped' else room),kind


def apply_snapshots(archive, paths, changed, entries, *, source_id):
    """Commit rows, revision, feed and snapshot checkpoints together."""
    archive = Path(archive)
    safe_sqlite_files(archive)
    with closing(sqlite3.connect(archive.resolve().as_uri()+'?mode=ro',uri=True)) as prior:
        if _get(prior,'source_id')!=source_id:raise ValueError('output_belongs_to_another_source')
    with closing(sqlite3.connect(archive,timeout=30)) as db:
        db.execute('PRAGMA journal_mode=WAL');db.execute('PRAGMA synchronous=FULL')
        if _get(db,'source_id')!=source_id:raise ValueError('output_belongs_to_another_source')
        db.execute('CREATE TABLE IF NOT EXISTS message_revisions(message_key TEXT,fingerprint TEXT,raw_json TEXT,observed_at TEXT,PRIMARY KEY(message_key,fingerprint))')
        names,mapping,senders=_mapping(paths,db)
        affected=set();captured=now();counts={'inserted':0,'updated':0,'deleted':0}
        contacts_changed='contact/contact.db' in changed or 'session/session.db' in changed
        scanned=set(paths) if contacts_changed else set(changed)
        with db:
            for name in sorted(scanned):
                with closing(ro(paths[name])) as source:
                    for table in tables(source):
                        if not table.startswith('Msg_'):continue
                        needed={'local_id','server_id','local_type','create_time','real_sender_id','message_content'}
                        if not needed.issubset(columns(source,table)):raise ValueError('unsupported_message_schema')
                        room,title,kind=_room(table,mapping,names)
                        db.execute('INSERT INTO groups VALUES(?,?,?,0) ON CONFLICT(room_id) DO UPDATE SET name=excluded.name,kind=excluded.kind',
                                   (room,title,kind))
                        old={row[0]:row[1] for row in db.execute('SELECT local_id,fingerprint FROM source_fingerprints WHERE source_db=? AND source_table=?',(name,table))}
                        for row in source.execute('SELECT * FROM '+ident(table)):
                            data=dict(row);local=int(data['local_id']);server=str(data['server_id'])
                            raw=raw_json(row);digest=hashlib.sha256(raw.encode()).hexdigest()
                            key=f'{room}:server:{server}' if server.isdecimal() and int(server)>0 else f'{room}:{name}:{table}:local:{local}'
                            prior=db.execute('SELECT message_key FROM source_records WHERE source_db=? AND source_table=? AND local_id=?',(name,table,local)).fetchone()
                            if old.get(local)==digest and prior and prior[0]==key and not contacts_changed:continue
                            if prior:affected.add(prior[0])
                            affected.add(key)
                            db.execute('INSERT INTO source_records VALUES(?,?,?,?,?) ON CONFLICT(source_db,source_table,local_id) DO UPDATE SET message_key=excluded.message_key,raw_json=excluded.raw_json',
                                       (name,table,local,key,raw))
                            db.execute('INSERT INTO source_fingerprints VALUES(?,?,?,?) ON CONFLICT(source_db,source_table,local_id) DO UPDATE SET fingerprint=excluded.fingerprint',
                                       (name,table,local,digest))
            for key in sorted(affected):
                previous=db.execute('SELECT * FROM messages WHERE message_key=?',(key,)).fetchone()
                winner=db.execute('SELECT source_db,source_table,local_id,raw_json FROM source_records WHERE message_key=? ORDER BY source_db DESC,local_id DESC,source_table DESC LIMIT 1',(key,)).fetchone()
                if winner:
                    name,table,local,raw=winner;data=_data(raw)
                    room,title,kind=_room(table,mapping,names)
                    db.execute('INSERT INTO groups VALUES(?,?,?,0) ON CONFLICT(room_id) DO UPDATE SET name=excluded.name,kind=excluded.kind',(room,title,kind))
                    sender=senders.get(name,{}).get(data['real_sender_id'],'')
                    content,state,base=decode(data,sender,kind)
                    values=(key,room,sender,names.get(sender,sender) if sender else '发送者未映射',data['create_time'],base,content,state,
                            name,table,local,str(data['server_id']),raw,captured)
                    if previous and previous[:-1]==values[:-1]:continue
                    if previous:
                        db.execute('INSERT OR IGNORE INTO message_revisions VALUES(?,?,?,?)',(key,hashlib.sha256(previous[12].encode()).hexdigest(),previous[12],captured))
                    db.execute('INSERT OR REPLACE INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',values)
                    counts['updated' if previous else 'inserted']+=1;operation='upsert'
                elif previous:
                    db.execute('INSERT OR IGNORE INTO message_revisions VALUES(?,?,?,?)',(key,hashlib.sha256(previous[12].encode()).hexdigest(),previous[12],captured))
                    db.execute('DELETE FROM messages WHERE message_key=?',(key,));counts['deleted']+=1;operation='delete'
                else:continue
                db.execute('INSERT INTO change_feed(message_key,operation) VALUES(?,?)',(key,operation))
            db.execute('UPDATE groups SET source_rows=(SELECT count(*) FROM source_records r JOIN messages m ON m.message_key=r.message_key WHERE m.room_id=groups.room_id)')
            stats=json.loads(_get(db,'status','{}'));kinds=dict(db.execute('SELECT kind,count(*) FROM groups GROUP BY kind'))
            total,oldest,newest=db.execute('SELECT count(*),min(create_time),max(create_time) FROM messages').fetchone()
            raw_count=db.execute('SELECT count(*) FROM source_records').fetchone()[0]
            stats.update(updated_at=captured,state='continuous_archive',conversations=sum(kinds.values()),archived_messages=total,
                         source_rows=raw_count,source_unique_messages=total,duplicate_source_rows=raw_count-total,
                         groups=kinds.get('group',0),private_chats=kinds.get('private',0),service_chats=kinds.get('service',0),
                         system_chats=kinds.get('system',0),unmapped_chats=kinds.get('unmapped',0),oldest_timestamp=oldest,newest_timestamp=newest,
                         unparsed_messages=db.execute("SELECT count(*) FROM messages WHERE parse_state LIKE '%unparsed%'").fetchone()[0],
                         contact_count=len(names),database_count=len(entries),authenticated_snapshots=len(entries),
                         extension_required_snapshots=sum(item['state']=='extension_required' for item in entries.values()),
                         scope='all_observed_local_message_tables_retaining_source_history')
            revision=int(_get(db,'revision','0'))+1
            _set(db,'status',json.dumps(stats,ensure_ascii=False));_set(db,'collector_state',json.dumps(entries));_set(db,'revision',revision)
        return {'state':'updated','revision':revision,'archived_messages':total,'conversations':sum(kinds.values()),
                'changed_databases':len(changed),**counts}


def _cleanup(output, previous, current):
    """Remove only this collector's superseded snapshot files, never user archives."""
    current_paths={entry['path'] for entry in current.values()}
    for entry in previous.values():
        if entry['path'] in current_paths:continue
        file=relative_database(output,entry['path']);parts=file.relative_to(output).parts
        if len(parts)<4 or parts[0]!='snapshots':continue
        generation=output/'snapshots'/parts[1]
        if not (generation/'.collector-owned').is_file():continue
        file.unlink(missing_ok=True)
    for generation in (output/'snapshots').iterdir():
        if not generation.is_dir() or generation.is_symlink() or not (generation/'.collector-owned').is_file():continue
        if not any(relative_database(output,entry['path']).is_relative_to(generation) for entry in current.values()):
            shutil.rmtree(generation)


def collect(source, output, payload):
    separated(source,output);source=Path(source).expanduser().resolve();output=private_directory(Path(output).expanduser().resolve())
    source_id=hashlib.sha256(os.fsencode(source)).hexdigest();archive=output/'archive.sqlite'
    safe_sqlite_files(archive)
    with CollectorLock(output/'.lock'):
        records=databases(source);keys=validated_keys(records,payload);previous={}
        if archive.exists():
            with closing(sqlite3.connect(archive.resolve().as_uri()+'?mode=ro',uri=True)) as db:
                has_meta=db.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='archive_meta'").fetchone()
                if has_meta:
                    if _get(db,'source_id')!=source_id:raise ValueError('output_belongs_to_another_source')
                    previous=json.loads(_get(db,'collector_state','{}'))
        if set(previous)-{name for name,_,_ in records}:raise ValueError('previous_source_database_missing')
        signatures={name:fingerprint(path) for name,path,_ in records}
        epoch=time.time()
        changed=[name for name,_,_ in records if name not in previous or previous[name]['fingerprint']!=signatures[name]
                 or epoch-previous[name].get('verified_epoch',0)>=3600 or not relative_database(output,previous[name]['path']).is_file()]
        if not changed and archive.exists():
            with closing(sqlite3.connect(archive.resolve().as_uri()+'?mode=ro',uri=True)) as db:
                status=json.loads(_get(db,'status'));revision=int(_get(db,'revision','0'))
            return {'state':'idle','revision':revision,'changed_databases':0,'inserted':0,'updated':0,'deleted':0,
                    'archived_messages':status['archived_messages'],'conversations':status['conversations']}
        generation=private_directory(output/'snapshots'/uuid.uuid4().hex)
        (generation/'.collector-owned').touch(mode=0o600)
        entries=dict(previous);committed=False
        try:
            for name,path,_ in records:
                if name not in changed:continue
                target=relative_database(generation/'databases',name);private_directory(target.parent)
                try:
                    metrics=snapshot_database(path,target,keys[name],cipher_module=load_cipher());state='ok'
                except SnapshotError:
                    if not name.endswith('_fts.db'):raise
                    metrics=snapshot_extension_index(path,target,keys[name]);state='extension_required'
                if fingerprint(path)!=signatures[name]:raise ValueError('source_changed_after_snapshot')
                entries[name]={'path':target.relative_to(output).as_posix(),'fingerprint':signatures[name],
                               'verified_epoch':epoch,'state':state,'metrics':metrics}
            paths=_paths(output,entries)
            if not previous:
                # Initial snapshot contains all databases; reuse the accepted full parser once.
                manifest={'database_count':len(entries),'databases':[{'database':name,'state':entry['state']} for name,entry in entries.items()],
                          'source_id':source_id,'collector_state':entries,'verified_count':sum(e['state']=='ok' for e in entries.values()),
                          'authenticated_count':len(entries),'failed_count':0,'extension_required_count':sum(e['state']=='extension_required' for e in entries.values()),
                          'wal_committed_frames':sum(e['metrics']['committed_frames'] for e in entries.values())}
                atomic_json(generation/'snapshot-status.json',manifest)
                stats=parse_snapshots(generation,output)
                result={'state':'updated','revision':0,'archived_messages':stats['archived_messages'],'conversations':stats['conversations'],
                        'changed_databases':len(changed),'inserted':stats['archived_messages'],'updated':0,'deleted':0}
            else:
                result=apply_snapshots(archive,paths,changed,entries,source_id=source_id)
            committed=True
            try:_cleanup(output,previous,entries)
            except OSError:
                # A committed archive is authoritative; cleanup is retried next tick.
                result['cache_cleanup_pending']=True
            return result
        finally:
            if not committed:
                # Publishing may succeed before a later cleanup failure. Preserve
                # snapshots referenced by the durable checkpoint in that case.
                referenced=False
                if archive.exists():
                    safe_sqlite_files(archive)
                    with closing(sqlite3.connect(archive.resolve().as_uri()+'?mode=ro',uri=True)) as check:
                        if check.execute("SELECT 1 FROM sqlite_master WHERE name='archive_meta'").fetchone():
                            saved=json.loads(_get(check,'collector_state','{}'))
                            referenced=any(relative_database(output,e['path']).is_relative_to(generation) for e in saved.values())
                if not referenced:shutil.rmtree(generation)
