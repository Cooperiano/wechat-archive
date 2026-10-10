"""Synthetic fixtures only: never discover or read a user's WeChat account."""
from contextlib import closing
import hashlib
import hmac
import json
import os
from pathlib import Path
import sqlite3
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch
import zstandard
from wechat_archive import cipher, cli, parser, storage, codec
from wechat_archive.platform_runtime import CollectorLock


def fixtures(root, count=201, source_id='synthetic-account-a'):
    folder = root / 'databases/contact'; folder.mkdir(parents=True)
    with closing(sqlite3.connect(folder/'contact.db')) as db, db:
        db.execute('CREATE TABLE contact(username TEXT, remark TEXT, nick_name TEXT)')
        for user in ['demo@chatroom', 'demo', 'gh_demo', 'filehelper']:
            db.execute('INSERT INTO contact VALUES(?,?,?)', (user, user, user))
    folder = root / 'databases/message'; folder.mkdir(parents=True)
    with closing(sqlite3.connect(folder/'message_0.db')) as db, db:
        db.execute('CREATE TABLE Name2Id(user_name TEXT)')
        db.execute("INSERT INTO Name2Id VALUES('demo')")
        for user in ['demo@chatroom', 'demo', 'gh_demo', 'filehelper', 'unmapped-demo']:
            table = 'Msg_' + hashlib.md5(user.encode()).hexdigest()
            db.execute('CREATE TABLE '+table+'(local_id INTEGER,server_id INTEGER,local_type INTEGER,create_time INTEGER,real_sender_id INTEGER,message_content BLOB)')
            n = count if user == 'demo' else 1
            for i in range(n):
                db.execute('INSERT INTO '+table+' VALUES(?,?,?,?,?,?)', (i+1, i+1, 1, 1700000000+i, 1, '<script>synthetic</script> '+str(i)))
    manifest = {'database_count':2,'verified_count':2,'authenticated_count':2,'failed_count':0,'wal_committed_frames':0,'source_id':source_id,
                'databases':[{'database':'contact/contact.db','state':'ok'},{'database':'message/message_0.db','state':'ok'}]}
    (root/'snapshot-status.json').write_text(json.dumps(manifest))
    return root


class CoreTests(unittest.TestCase):
    def test_database_walk_reports_permission_denial(self):
        from wechat_archive.pipeline import databases
        def walk(*args, **kwargs):
            kwargs['onerror'](PermissionError('synthetic denied directory'))
            return iter(())
        with patch('wechat_archive.pipeline.os.walk', side_effect=walk):
            with self.assertRaises(PermissionError):
                databases(Path(tempfile.gettempdir()) / 'synthetic-private-source')
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.root=Path(self.temp.name)
    def tearDown(self):
        self.temp.cleanup()
    def test_complete_parser_all_chat_kinds_and_raw_retention(self):
        source=fixtures(self.root/'input'); out=self.root/'output'
        stats=parser.parse_snapshots(source,out)
        self.assertEqual((stats['conversations'],stats['archived_messages'],stats['missing_source_messages']), (5,205,0))
        self.assertEqual((stats['groups'],stats['private_chats'],stats['service_chats'],stats['system_chats'],stats['unmapped_chats']), (1,1,1,1,1))
        with closing(sqlite3.connect(out/'archive.sqlite')) as db:
            self.assertEqual(db.execute('SELECT count(*) FROM source_records').fetchone()[0],205)
            self.assertIn('<script>',db.execute('SELECT content FROM messages LIMIT 1').fetchone()[0])
    def test_different_source_does_not_replace_archive(self):
        a=fixtures(self.root/'a'); b=fixtures(self.root/'b',source_id='synthetic-account-b'); out=self.root/'out'
        parser.parse_snapshots(a,out); original=(out/'archive.sqlite').read_bytes()
        with self.assertRaisesRegex(ValueError,'another_source'): parser.parse_snapshots(b,out)
        self.assertEqual(original,(out/'archive.sqlite').read_bytes())
    def test_metadata_failure_preserves_entire_previous_archive(self):
        source=fixtures(self.root/'in');out=self.root/'out'
        parser.parse_snapshots(source,out)
        original={p.name:p.read_bytes() for p in out.iterdir() if p.is_file()}
        for failure in ['database-catalog.json','status.json','source-info.json']:
            write=parser.atomic_json
            def fail(path,value):
                if Path(path).name==failure:raise OSError('synthetic disk failure')
                return write(path,value)
            with patch.object(parser,'atomic_json',side_effect=fail):
                with self.assertRaises(OSError):parser.parse_snapshots(source,out)
            self.assertEqual(original,{p.name:p.read_bytes() for p in out.iterdir() if p.is_file()})
    def test_commit_failure_rolls_back_metadata_and_database(self):
        source=fixtures(self.root/'in');out=self.root/'out';parser.parse_snapshots(source,out)
        original={p.name:p.read_bytes() for p in out.iterdir() if p.is_file()}
        replace=os.replace
        def fail(src,dst):
            if Path(dst).resolve()==(out/'archive.sqlite').resolve() and Path(src).parent.name.startswith('.publish-'):
                raise OSError('synthetic commit failure')
            return replace(src,dst)
        with patch.object(parser.os,'replace',side_effect=fail):
            with self.assertRaises(OSError):parser.parse_snapshots(source,out)
        self.assertEqual(original,{p.name:p.read_bytes() for p in out.iterdir() if p.is_file()})
    def test_double_write_failure_retains_backup_and_attempts_other_rollbacks(self):
        source=fixtures(self.root/'in');out=self.root/'out';parser.parse_snapshots(source,out)
        original=(out/'contacts.json').read_bytes();replace=os.replace
        def fail(src,dst):
            if Path(dst).resolve()==(out/'archive.sqlite').resolve() and Path(src).parent.name.startswith('.publish-'):
                raise OSError('synthetic commit failure')
            if Path(src).parent.name=='previous' and Path(src).name=='status.json':
                raise OSError('synthetic rollback failure')
            return replace(src,dst)
        with patch.object(parser.os,'replace',side_effect=fail):
            with self.assertRaisesRegex(ValueError,'archive_recovery_required'):parser.parse_snapshots(source,out)
        self.assertEqual(original,(out/'contacts.json').read_bytes())
        retained=list(out.glob('.publish-*/previous/status.json'))
        self.assertEqual(len(retained),1)
    def test_manifest_traversal_rejected_before_output(self):
        source=fixtures(self.root/'in'); p=source/'snapshot-status.json'; data=json.loads(p.read_text())
        data['databases'][0]['database']='../../outside.db';p.write_text(json.dumps(data))
        with self.assertRaises(ValueError):parser.parse_snapshots(source,self.root/'out')
        self.assertFalse((self.root/'out').exists())
    def test_cli_overlap_rejected_before_write(self):
        source=self.root/'source';source.mkdir();mode=source.stat().st_mode
        with self.assertRaises(ValueError):cli.execute(['archive','--source',str(source),'--output',str(source),'--keys-json','unused.json'])
        self.assertEqual(mode,source.stat().st_mode);self.assertEqual(list(source.iterdir()),[])
    def test_expanduser_source_boundary(self):
        with patch.dict(os.environ,{'HOME':str(self.root)}):
            with self.assertRaises(ValueError):storage.separated('~/account','~/account/output')
    @unittest.skipIf(os.name != 'posix','POSIX symlink fixture')
    def test_lock_symlink_rejected_without_modifying_source(self):
        source=self.root/'source';source.write_bytes(b'synthetic');source.chmod(0o644)
        (self.root/'.lock').symlink_to(source)
        with self.assertRaises(ValueError):CollectorLock(self.root/'.lock')
        self.assertEqual(source.stat().st_mode&0o777,0o644)
    def test_fts_helper_source_alias_protection(self):
        source=self.root/'source.db';source.write_bytes(b'synthetic')
        with self.assertRaises(ValueError):parser.snapshot_extension_index(source,source,b'x'*32)
        target=self.root/'alias.db';os.link(source,target)
        with self.assertRaises(ValueError):parser.snapshot_extension_index(source,target,b'x'*32)
        self.assertEqual(source.read_bytes(),b'synthetic')
    def test_compression_and_malformed_content(self):
        value=zstandard.ZstdCompressor().compress('合成正文'.encode())
        self.assertEqual(codec.decode_text(value),('合成正文','decoded'))
        self.assertEqual(codec.decode_text(b'\x28\xb5\x2f\xfdwrong')[1],'compressed_unparsed')
    def test_page_authentication_rejects_wrong_key_and_tamper(self):
        key=b'SYNTHETIC-ONLY-PAGE-TEST-KEY-000';self.assertEqual(len(key),32)
        page=bytearray(4096);page[:16]=bytes(range(16))
        mac=hashlib.pbkdf2_hmac('sha512',key,bytes(x^0x3a for x in page[:16]),2,32)
        page[-64:]=hmac.new(mac,bytes(page[16:-64])+struct.pack('<I',1),hashlib.sha512).digest()
        self.assertTrue(cipher.verify_enc_key(key,bytes(page)))
        self.assertFalse(cipher.verify_enc_key(b'Y'*32,bytes(page)))
        page[40]^=1;self.assertFalse(cipher.verify_enc_key(key,bytes(page)))
    @unittest.skipUnless(sys.platform in {'darwin','win32'},'requires native AES')
    def test_native_aes_nist_public_vector(self):
        # Public AES-256-CBC example, not any user's encryption key.
        key=bytes.fromhex('603deb1015ca71be2b73aef0857d77811f352c073b6108d72d9810a30914dff4')
        iv=bytes.fromhex('000102030405060708090a0b0c0d0e0f')
        encrypted=bytes.fromhex('f58c4c04d6e5f1ba779eabfb5f7bfbd6')
        self.assertEqual(cipher.aes_cbc_decrypt(key,iv,encrypted).hex(),'6bc1bee22e409f96e93d7e117393172a')


if __name__=='__main__':unittest.main()
