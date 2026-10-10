"""Synthetic process and recovery tests; no real app or key capture."""
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import types
import unittest
from unittest.mock import patch, Mock
from wechat_archive import bootstrap, storage, security

class BootstrapTests(unittest.TestCase):
    @unittest.skipIf(os.name!='posix','symlink fixture')
    def test_copy_processes_matches_canonical_alias(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory);real=root/'real';real.mkdir();alias=root/'alias';alias.symlink_to(real,target_is_directory=True)
            copy=alias/'WeChat.app'
            output=f'123 {real}/WeChat.app/Contents/MacOS/WeChat\n456 /Applications/WeChat.app/Contents/MacOS/WeChat\n'
            with patch.object(bootstrap,'run',return_value=types.SimpleNamespace(stdout=output.encode())):
                self.assertEqual(bootstrap.copy_processes(copy),[123])
    def test_attach_failure_still_recovers_official(self):
        device=Mock();device.spawn.return_value=123;device.attach.side_effect=RuntimeError('synthetic attach failure')
        commands=[]
        def run(command,*args):commands.append(command);return Mock()
        with patch.object(bootstrap.sys,'platform','darwin'), patch.dict(sys.modules,{'frida':types.SimpleNamespace(get_local_device=lambda:device)}), patch.object(bootstrap,'databases',return_value=[('demo.db',None,b'x'*4096)]), patch.object(bootstrap,'app_identity',return_value={'CDHash':'synthetic'}), patch.object(bootstrap,'running_binary',side_effect=[True,False,False,True,True]), patch.object(bootstrap,'copy_processes',return_value=[]), patch.object(bootstrap,'stop_copy') as stop, patch.object(bootstrap,'run',side_effect=run):
            with self.assertRaises(RuntimeError):bootstrap.mac_keys(Path('/synthetic'),allow_restart=True)
            stop.assert_called_once()
        self.assertTrue(any(command[0]=='/usr/bin/open' for command in commands))
    @unittest.skipUnless(sys.platform=='win32','native Windows DPAPI')
    def test_windows_dpapi_synthetic_roundtrip(self):
        payload=b'public synthetic credential fixture'
        encrypted=security._transform(payload,False)
        self.assertNotIn(payload,encrypted)
        self.assertEqual(security._transform(encrypted,True),payload)
    @unittest.skipUnless(sys.platform=='win32','native Windows ACL')
    def test_windows_explicit_shared_acl_removed(self):
        with tempfile.TemporaryDirectory() as directory:
            root=Path(directory)/'private';root.mkdir()
            subprocess.run(['icacls',str(root),'/grant','*S-1-1-0:(OI)(CI)F'],capture_output=True,check=True)
            storage.private_directory(root)
            result=subprocess.run(['icacls',str(root)],capture_output=True,check=True).stdout.decode(errors='replace')
            self.assertNotIn('Everyone',result)
            self.assertNotIn('S-1-1-0',result)

if __name__=='__main__':unittest.main()
