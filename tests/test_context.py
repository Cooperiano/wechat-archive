"""Only synthetic archives are used here."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from wechat_archive import context


def archive(path, *, source_id='a' * 64, generation='generation-1'):
    with closing(sqlite3.connect(path)) as db, db:
        db.execute('PRAGMA journal_mode=WAL')
        db.execute('CREATE TABLE groups(room_id TEXT PRIMARY KEY,name TEXT,kind TEXT,source_rows INTEGER)')
        db.execute("INSERT INTO groups VALUES('demo','Synthetic','private',0)")
        db.execute('''CREATE TABLE messages(message_key TEXT PRIMARY KEY,room_id TEXT,sender_id TEXT,
         sender_name TEXT,create_time INTEGER,local_type INTEGER,content TEXT,parse_state TEXT,
         source_db TEXT,source_table TEXT,local_id INTEGER,server_id TEXT,raw_json TEXT,archived_at TEXT)''')
        db.execute('CREATE TABLE archive_meta(key TEXT PRIMARY KEY,value TEXT)')
        db.executemany('INSERT INTO archive_meta VALUES(?,?)', [('source_id', source_id),('generation',generation)])
        db.execute('CREATE TABLE change_feed(seq INTEGER PRIMARY KEY AUTOINCREMENT,message_key TEXT,operation TEXT)')
        for n, body in enumerate(('alpha synthetic', 'beta synthetic'), 1):
            db.execute('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                       (f'key-{n}','demo','sender','Synthetic Sender',1700000000,1,body,'decoded',
                        'synthetic.db','Msg_synthetic',n,str(n),'PRIVATE RAW', '2023-01-01'))


class ContextTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(dir=Path.cwd())
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'archive.sqlite'
        self.output = self.root / 'private'
        archive(self.source)

    def drain(self, batch_size=1):
        result = None
        for _ in range(20):
            result = context.process_archive(self.source,self.output,batch_size=batch_size)
            if not result['pending']:
                return result
        self.fail('context did not drain')

    def test_baseline_same_timestamp_days_evidence_and_restart(self):
        first = self.drain()
        self.assertEqual((first['indexed_messages'],first['days'],first['baseline_complete'],first['feed_cursor']),
                         (2,1,True,0))
        with closing(sqlite3.connect(self.output/'context.sqlite')) as db:
            self.assertEqual(db.execute('SELECT message_count FROM context_days').fetchone()[0],2)
            self.assertEqual(db.execute('SELECT local_day FROM context_days').fetchone()[0],'2023-11-15')
            self.assertNotIn('PRIVATE RAW',str(db.execute('SELECT * FROM context_messages').fetchall()))
        second = context.process_archive(self.source,self.output,batch_size=1)
        self.assertEqual((second['indexed'],second['updated'],second['deleted']), (0,0,0))
        self.assertEqual(len(context.search_context(self.output,'synthetic')),2)
        self.assertEqual(context.search_context(self.output,'" OR *'),[])

    def test_edit_delete_and_durable_cursor(self):
        context.process_archive(self.source,self.output)
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("UPDATE messages SET content='corrected evidence' WHERE message_key='key-1'")
            db.execute("INSERT INTO change_feed(message_key,operation) VALUES('key-1','upsert')")
            db.execute("DELETE FROM messages WHERE message_key='key-2'")
            db.execute("INSERT INTO change_feed(message_key,operation) VALUES('key-2','delete')")
        result=self.drain()
        self.assertEqual((result['indexed_messages'],result['feed_cursor'],result['deleted']), (1,2,1))
        self.assertEqual([r['message_key'] for r in context.search_context(self.output,'corrected')],['key-1'])
        self.assertEqual(context.search_context(self.output,'beta'),[])
        self.assertEqual(context.process_archive(self.source,self.output)['feed_cursor'],2)

    def test_failed_batch_does_not_advance_cursor(self):
        context.process_archive(self.source,self.output)
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("INSERT INTO change_feed(message_key,operation) VALUES('key-1','upsert')")
        with patch.object(context,'_upsert',side_effect=RuntimeError('synthetic crash')):
            with self.assertRaises(RuntimeError):
                context.process_archive(self.source,self.output)
        with closing(sqlite3.connect(self.output/'context.sqlite')) as db:
            self.assertEqual(db.execute("SELECT value FROM state WHERE key='feed_cursor'").fetchone()[0],'0')
        self.assertEqual(context.process_archive(self.source,self.output)['feed_cursor'],1)

    def test_source_binding_and_generation_rebuild(self):
        context.process_archive(self.source,self.output)
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("UPDATE archive_meta SET value=? WHERE key='source_id'", ('b'*64,))
        with self.assertRaisesRegex(ValueError,'another_source'):
            context.process_archive(self.source,self.output)
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("UPDATE archive_meta SET value=? WHERE key='source_id'", ('a'*64,))
            db.execute("UPDATE archive_meta SET value='generation-2' WHERE key='generation'")
            db.execute("DELETE FROM messages WHERE message_key='key-2'")
        self.assertEqual(context.process_archive(self.source,self.output)['indexed_messages'],1)

    def test_baseline_bookmark_replays_later_change(self):
        original = context._upsert
        fired = False
        def concurrent(db,row,fts,dirty):
            nonlocal fired
            if not fired:
                fired=True
                with closing(sqlite3.connect(self.source)) as writer, writer:
                    writer.execute("UPDATE messages SET content='late correction' WHERE message_key='key-1'")
                    writer.execute("INSERT INTO change_feed(message_key,operation) VALUES('key-1','upsert')")
            return original(db,row,fts,dirty)
        with patch.object(context,'_upsert',side_effect=concurrent):
            result=self.drain()
        self.assertEqual(result['feed_cursor'],1)
        self.assertEqual(len(context.search_context(self.output,'late correction')),1)

    def test_batch_is_bounded_and_status_is_pending(self):
        first=context.process_archive(self.source,self.output,batch_size=1)
        self.assertEqual((first['processed'],first['indexed_messages'],first['pending']), (1,1,1))
        self.assertEqual(first['state'],'pending')
        self.assertEqual(self.drain()['indexed_messages'],2)

    def test_invalid_output_does_not_change_source_parent(self):
        before=self.root.stat().st_mode
        with self.assertRaisesRegex(ValueError,'source_output_overlap'):
            context.process_archive(self.source,self.root)
        self.assertEqual(self.root.stat().st_mode,before)
        self.assertFalse((self.root/'context.sqlite').exists())

    def test_missing_fts_reports_degraded(self):
        with patch.object(context,'_init',side_effect=lambda db: (db.executescript(context._SCHEMA),False)[1]):
            result=self.drain()
        self.assertEqual(result['state'],'index-degraded')
        self.assertEqual(result['indexed_messages'],2)
        self.assertEqual(context.search_context(self.output,'synthetic'),[])

    def test_initial_binding_failure_leaves_no_partial_target(self):
        original=context._source_id
        calls=0
        def fail_second(source,path):
            nonlocal calls
            calls += 1
            if calls == 2:
                raise RuntimeError('synthetic initialization interruption')
            return original(source,path)
        with patch.object(context,'_source_id',side_effect=fail_second):
            with self.assertRaises(RuntimeError):
                context.process_archive(self.source,self.output)
        self.assertFalse((self.output/'context.sqlite').exists())
        self.assertEqual(list(self.output.glob('.context-init-*.sqlite')),[])
        self.assertEqual(self.drain()['indexed_messages'],2)

    def test_change_feed_gap_rejected_before_cursor_commit(self):
        self.drain()
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("UPDATE messages SET content='gap correction' WHERE message_key='key-1'")
            db.execute("INSERT INTO change_feed(seq,message_key,operation) VALUES(1,'key-1','upsert')")
            db.execute("INSERT INTO change_feed(seq,message_key,operation) VALUES(3,'key-2','delete')")
        with self.assertRaisesRegex(ValueError,'change_feed_history_lost'):
            context.process_archive(self.source,self.output)
        with closing(sqlite3.connect(self.output/'context.sqlite')) as db:
            self.assertEqual(db.execute("SELECT value FROM state WHERE key='feed_cursor'").fetchone()[0],'0')
            self.assertEqual(db.execute("SELECT content FROM context_messages WHERE message_key='key-1'").fetchone()[0],
                             'alpha synthetic')

    def test_fts_recovers_with_bounded_reindex(self):
        with patch.object(context,'_init',side_effect=lambda db: (db.executescript(context._SCHEMA),False)[1]):
            self.drain()
        first=context.process_archive(self.source,self.output,batch_size=1)
        self.assertEqual((first['state'],first['pending']),('pending',1))
        self.assertEqual(context.search_context(self.output,'synthetic'),[])
        complete=self.drain()
        self.assertEqual(complete['state'],'complete')
        self.assertEqual(len(context.search_context(self.output,'synthetic')),2)
        with closing(sqlite3.connect(self.output/'context.sqlite')) as db:
            self.assertEqual(db.execute('''SELECT count(*) FROM context_fts f
              JOIN context_messages m ON f.rowid=m.rowid AND f.message_key=m.message_key''').fetchone()[0],2)

    def test_large_synthetic_baseline_preserves_fts_rowids(self):
        with closing(sqlite3.connect(self.source)) as db, db:
            db.executemany('INSERT INTO messages VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)',
                           ((f'bulk-{n}','demo','sender','Synthetic Sender',1700000000+n,1,
                             'bulk synthetic','decoded','synthetic.db','Msg_synthetic',n,str(n),
                             'PRIVATE RAW','2023-01-01') for n in range(3,4001)))
        with patch.object(context,'_init',side_effect=lambda db: (db.executescript(context._SCHEMA),False)[1]):
            degraded=self.drain(batch_size=1000)
        self.assertEqual(degraded['state'],'index-degraded')
        result=self.drain(batch_size=1000)
        self.assertEqual((result['indexed_messages'],result['state']), (4000,'complete'))
        with closing(sqlite3.connect(self.output/'context.sqlite')) as db:
            self.assertEqual(db.execute('''SELECT count(*) FROM context_fts f
              JOIN context_messages m ON f.rowid=m.rowid AND f.message_key=m.message_key''').fetchone()[0],4000)

    def test_rejects_arbitrary_or_linked_target_without_writes(self):
        self.output.mkdir()
        target=self.output/'context.sqlite'
        with closing(sqlite3.connect(target)) as db, db:
            db.execute('CREATE TABLE unrelated(secret TEXT)')
            db.execute("INSERT INTO unrelated VALUES('synthetic keep')")
        before=target.read_bytes()
        with self.assertRaisesRegex(ValueError,'invalid_context_schema'):
            context.process_archive(self.source,self.output)
        self.assertEqual(target.read_bytes(),before)
        target.unlink()
        other=self.root/'other.sqlite'
        with closing(sqlite3.connect(other)) as db, db:
            db.execute('CREATE TABLE unrelated(secret TEXT)')
        target.hardlink_to(other)
        before=other.read_bytes()
        with self.assertRaisesRegex(ValueError,'unsafe_context_file'):
            context.process_archive(self.source,self.output)
        self.assertEqual(other.read_bytes(),before)

    def test_legacy_replacement_reconciles(self):
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute('DROP TABLE change_feed')
            db.execute('DROP TABLE archive_meta')
        (self.root/'source-info.json').write_text('{"source_id":"legacy"}')
        context.process_archive(self.source,self.output)
        with closing(sqlite3.connect(self.source)) as db, db:
            db.execute("DELETE FROM messages WHERE message_key='key-2'")
        self.assertEqual(context.process_archive(self.source,self.output)['indexed_messages'],1)


if __name__ == '__main__':
    unittest.main()
