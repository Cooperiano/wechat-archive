"""Plaintext synthetic snapshot diffs only; no device profile discovery."""
from contextlib import closing
import hashlib,json,os,shutil
from pathlib import Path
import sqlite3,tempfile,unittest
from unittest.mock import patch
from wechat_archive import incremental,parser
from test_core import fixtures

class IncrementalTests(unittest.TestCase):
 def setUp(self):
  self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
  self.source=fixtures(self.root/'input',count=3);self.out=self.root/'out'
  parser.parse_snapshots(self.source,self.out)
  self.archive=self.out/'archive.sqlite';self.paths={p.relative_to(self.source/'databases').as_posix():p for p in (self.source/'databases').rglob('*.db')}
  self.table='Msg_'+hashlib.md5(b'demo').hexdigest();self.message=self.paths['message/message_0.db']
 def apply(self):
  return incremental.apply_snapshots(self.archive,self.paths,['message/message_0.db'],{},source_id='synthetic-account-a')
 def test_initial_metadata_and_unchanged_canonical_no_feed(self):
  result=self.apply();self.assertEqual(result['inserted']+result['updated'],0)
  with closing(sqlite3.connect(self.archive)) as db:self.assertEqual(db.execute('SELECT count(*) FROM change_feed').fetchone()[0],0)
 def test_late_same_timestamp_insert_and_old_edit_emit_feed(self):
  with closing(sqlite3.connect(self.message)) as db,db:
   db.execute('UPDATE '+self.table+' SET message_content=? WHERE local_id=1',('edited',))
   db.execute('INSERT INTO '+self.table+' VALUES(?,?,?,?,?,?)',(4,4,1,1700000000,1,'late'))
  result=self.apply();self.assertEqual((result['inserted'],result['updated']),(1,1))
  with closing(sqlite3.connect(self.archive)) as db:
   self.assertEqual(db.execute('SELECT count(*) FROM change_feed').fetchone()[0],2)
   self.assertEqual(db.execute('SELECT count(*) FROM message_revisions').fetchone()[0],1)
  second=self.apply();self.assertEqual((second['inserted'],second['updated']),(0,0))
 def test_local_to_server_identity_migration(self):
  with closing(sqlite3.connect(self.message)) as db,db:db.execute('UPDATE '+self.table+' SET server_id=0 WHERE local_id=1')
  a=self.apply();self.assertEqual((a['inserted'],a['deleted']),(1,1))
  with closing(sqlite3.connect(self.message)) as db,db:db.execute('UPDATE '+self.table+' SET server_id=99 WHERE local_id=1')
  b=self.apply();self.assertEqual((b['inserted'],b['deleted']),(1,1))
  with closing(sqlite3.connect(self.archive)) as db:self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0],7)
 def test_source_retention_does_not_erase_observed_history(self):
  with closing(sqlite3.connect(self.message)) as db,db:db.execute('DELETE FROM '+self.table+' WHERE local_id=1')
  self.assertEqual(self.apply()['deleted'],0)
  with closing(sqlite3.connect(self.archive)) as db:self.assertEqual(db.execute('SELECT count(*) FROM messages').fetchone()[0],7)
 def test_full_rebuild_rejects_mutable_wal_archive_with_reader(self):
  self.apply()
  with closing(sqlite3.connect(self.archive)) as reader:
   reader.execute('BEGIN');reader.execute('SELECT count(*) FROM messages').fetchone()
   with self.assertRaisesRegex(ValueError,'continuous_archive_rebuild_requires_new_output'):
    parser.parse_snapshots(self.source,self.out)
   self.assertEqual(reader.execute('SELECT count(*) FROM messages').fetchone()[0],7)
 def test_transaction_failure_rolls_back_rows_and_feed_and_cursor(self):
  with closing(sqlite3.connect(self.message)) as db,db:db.execute('UPDATE '+self.table+' SET message_content=? WHERE local_id=1',('changed',))
  with closing(sqlite3.connect(self.archive)) as db:before=dict(db.execute('SELECT * FROM archive_meta'))
  with patch.object(incremental,'_set',side_effect=OSError('synthetic transaction failure')):
   with self.assertRaises(OSError):self.apply()
  with closing(sqlite3.connect(self.archive)) as db:
   self.assertEqual(before,dict(db.execute('SELECT * FROM archive_meta')))
   self.assertEqual(db.execute('SELECT count(*) FROM change_feed').fetchone()[0],0)
   self.assertNotEqual(db.execute('SELECT content FROM messages WHERE local_id=1 AND room_id=?',('demo',)).fetchone()[0],'changed')
 def test_wrong_source_does_not_change_rows(self):
  with self.assertRaises(ValueError):incremental.apply_snapshots(self.archive,self.paths,list(self.paths),{},source_id='wrong')
  with closing(sqlite3.connect(self.archive)) as db:self.assertEqual(db.execute('PRAGMA journal_mode').fetchone()[0],'delete')
 def test_hardlinked_sqlite_sidecars_rejected_before_read_or_write(self):
  from wechat_archive import server,context
  for suffix in ('-wal','-shm','-journal'):
   victim=self.root/('victim'+suffix);victim.write_bytes(b'keep synthetic bytes')
   sidecar=Path(str(self.archive)+suffix);os.link(victim,sidecar)
   try:
    for operation in (self.apply,lambda:server.connect(self.archive),lambda:context._ro(self.archive),lambda:parser.parse_snapshots(self.source,self.out)):
     with self.assertRaisesRegex(ValueError,'unsafe_archive_file'):operation()
    self.assertEqual(victim.read_bytes(),b'keep synthetic bytes')
   finally:sidecar.unlink()
 def test_collect_changed_database_cache_and_failure_preserves_previous(self):
  target=self.root/'live-output'
  records=[(name,path,b'x'*4096) for name,path in self.paths.items()]
  def capture(source,destination,*args,**kwargs):
   shutil.copyfile(source,destination);return {'committed_frames':0}
  keys={name:b'synthetic'*4 for name in self.paths}
  with patch.object(incremental,'databases',return_value=records),patch.object(incremental,'validated_keys',return_value=keys),patch.object(incremental,'snapshot_database',side_effect=capture) as snapshot:
   first=incremental.collect(self.source,target,{})
   self.assertEqual(first['archived_messages'],7)
   self.assertEqual(incremental.collect(self.source,target,{})['state'],'idle')
   self.assertEqual(snapshot.call_count,2)
   with closing(sqlite3.connect(self.message)) as db,db:db.execute('UPDATE '+self.table+' SET message_content=? WHERE local_id=1',('new',))
   second=incremental.collect(self.source,target,{})
   self.assertEqual((second['changed_databases'],second['updated']),(1,1))
   self.assertEqual(snapshot.call_count,3)
   with closing(sqlite3.connect(self.message)) as db,db:db.execute('UPDATE '+self.table+' SET message_content=? WHERE local_id=1',('failed',))
   with patch.object(incremental,'snapshot_database',side_effect=OSError('synthetic capture failure')):
    with self.assertRaises(OSError):incremental.collect(self.source,target,{})
   with closing(sqlite3.connect(target/'archive.sqlite')) as db:
    self.assertEqual(db.execute('SELECT content FROM messages WHERE room_id=? AND local_id=1',('demo',)).fetchone()[0],'new')
   generations=list((target/'snapshots').iterdir());self.assertEqual(len(generations),2)
 def test_fingerprint_sidecar_and_content_change(self):
  file=self.root/'synthetic.bin';file.write_bytes(b'a'*4096);before=incremental.fingerprint(file)
  file.write_bytes(b'b'*4096);self.assertNotEqual(before,incremental.fingerprint(file))
  Path(str(file)+'-wal').write_bytes(b'changed');self.assertNotEqual(before,incremental.fingerprint(file))

if __name__=='__main__':unittest.main()
