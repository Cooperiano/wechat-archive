"""Synthetic user-session service tests; no launchctl or schtasks is invoked."""
import json
from pathlib import Path
import plistlib
import tempfile
import unittest
from unittest.mock import Mock, patch
import xml.etree.ElementTree as ET

from wechat_archive import service


class ServiceTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)

    def test_launch_agent_targets_current_venv_and_watch_config(self):
        python = self.root / 'venv' / 'bin' / 'python'
        config = self.root / 'runner.json'
        payload = service.launch_agent(config, python)
        parsed = plistlib.loads(plistlib.dumps(payload))
        self.assertEqual(parsed['ProgramArguments'],
                         [str(python), '-m', 'wechat_archive', 'watch', '--config', str(config)])
        self.assertTrue(parsed['RunAtLoad'])
        self.assertTrue(parsed['KeepAlive'])

    def test_windows_task_is_user_logon_without_elevation(self):
        config = self.root / 'config with space.json'
        python = self.root / 'venv' / 'Scripts' / 'python.exe'
        xml = service.windows_task(config, python, 'S-1-5-21-123-456-789-1001')
        tree = ET.fromstring(xml)
        ns = {'t': 'http://schemas.microsoft.com/windows/2004/02/mit/task'}
        self.assertEqual(tree.findtext('.//t:LogonTrigger/t:UserId', namespaces=ns),
                         'S-1-5-21-123-456-789-1001')
        self.assertEqual(tree.findtext('.//t:Principal/t:RunLevel', namespaces=ns),
                         'LeastPrivilege')
        self.assertEqual(tree.findtext('.//t:Principal/t:LogonType', namespaces=ns),
                         'InteractiveToken')
        self.assertEqual(tree.findtext('.//t:Exec/t:Command', namespaces=ns), str(python))
        self.assertIn('"' + str(config) + '"',
                      tree.findtext('.//t:Exec/t:Arguments', namespaces=ns))

    def test_uninstall_absent_macos_does_not_call_launchctl(self):
        with patch.object(service.sys, 'platform', 'darwin'), \
             patch.object(service.Path, 'home', return_value=self.root), \
             patch.object(service, '_run') as command:
            result = service.uninstall()
        self.assertEqual(result, {'state': 'not_installed', 'data_retained': True})
        command.assert_not_called()

    def test_uninstall_absent_windows_does_not_end_or_delete(self):
        absent = Mock(returncode=1, stdout=b'')
        with patch.object(service.sys, 'platform', 'win32'), \
             patch.object(service, '_run', return_value=absent) as command:
            result = service.uninstall()
        self.assertTrue(result['data_retained'])
        self.assertEqual(command.call_count, 1)
        self.assertEqual(command.call_args.args[0][:2], ['schtasks.exe', '/Query'])

    def test_install_keeps_venv_symlink_path(self):
        venv_python = self.root / 'venv' / 'bin' / 'python'
        config = self.root / 'runner.json'
        config.write_text(json.dumps({'source': str(self.root / 'source'),
                                      'output': str(self.root / 'output'),
                                      'key_store': str(self.root / 'keys.protected')}))
        with patch.object(service.sys, 'platform', 'darwin'), \
             patch.object(service.sys, 'executable', str(venv_python)), \
             patch.object(service.os, 'getuid', return_value=501, create=True), \
             patch.object(service.Path, 'home', return_value=self.root), \
             patch.object(service.Path, 'is_file', return_value=True), \
             patch.object(service, '_run') as command:
            result = service.install(config)
        target = self.root / 'Library' / 'LaunchAgents' / (service.LABEL + '.plist')
        saved = plistlib.loads(target.read_bytes())
        self.assertEqual(saved['ProgramArguments'][0], str(venv_python))
        self.assertEqual(result['state'], 'installed')
        self.assertEqual(command.call_count, 3)


if __name__ == '__main__':
    unittest.main()
