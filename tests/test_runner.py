"""Synthetic tests only: never read a live WeChat profile or protected key."""
import json
from pathlib import Path
import tempfile
import threading
import types
import sys
import unittest
from unittest.mock import patch

from wechat_archive.platform_runtime import AlreadyRunningError, CollectorLock
from wechat_archive.runner import _cycle, read_config, run


class RunnerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.source = self.root / 'source'
        self.source.mkdir()
        self.output = self.root / 'output'
        self.keys = self.root / 'keys.protected'
        self.keys.write_bytes(b'synthetic')
        self.config = self.root / 'runner.json'
        self.write_config()

    def write_config(self, **changes):
        value = {'source': str(self.source), 'output': str(self.output),
                 'key_store': str(self.keys), 'interval_seconds': 30}
        value.update(changes)
        self.config.write_text(json.dumps(value))

    def fake_modules(self, collect, process=None):
        incremental = types.ModuleType('wechat_archive.incremental')
        incremental.collect = collect
        context = types.ModuleType('wechat_archive.context')
        context.process_archive = process or (lambda *_a, **_kw: {'state': 'complete'})
        return patch.dict(sys.modules, {'wechat_archive.incremental': incremental,
                                        'wechat_archive.context': context})

    def test_config_rejects_bad_inputs_before_output_is_created(self):
        bad = ({'interval_seconds': 29}, {'interval_seconds': True},
               {'context_batch_size': 0}, {'context_batch_size': True},
               {'context_batch_size': 50_001},
               {'output': str(self.source / 'inside')},
               {'key_store': str(self.output / 'keys.protected')},
               {'context_output': str(self.root)},
               {'context_output': str(self.output / 'snapshots' / 'context')},
               {'source': 'relative'}, {'unexpected': 'value'})
        for changes in bad:
            with self.subTest(changes=changes):
                self.write_config(**changes)
                with self.assertRaises(ValueError):
                    read_config(self.config)
                self.assertFalse(self.output.exists())
        self.config.write_text('{malformed')
        with self.assertRaises(ValueError):
            read_config(self.config)
        self.assertFalse(self.output.exists())

    def test_cycle_retries_missing_source_and_keys_without_collecting(self):
        calls = []
        def collect(*_):
            calls.append(1)
        config = read_config(self.config)
        self.source.rmdir()
        self.assertEqual(_cycle(config, collect=collect, load=lambda _: {}, process=None)['reason'],
                         'source_unavailable')
        self.source.mkdir()
        self.keys.unlink()
        self.assertEqual(_cycle(config, collect=collect, load=lambda _: {}, process=None)['reason'],
                         'key_unavailable')
        self.assertEqual(calls, [])

    def test_context_may_be_inside_private_output(self):
        self.write_config(context_output=str(self.output / 'context'))
        config = read_config(self.config)
        self.assertEqual(config.context_output, (self.output / 'context').resolve())
        self.assertEqual(config.context_batch_size, 10_000)

    def test_config_cannot_be_overwritten_by_status_or_archive_output(self):
        original = self.config.read_text()
        for candidate in (self.output / 'watch-status.json',
                          self.output / 'snapshots' / 'runner.json',
                          self.source / 'runner.json',
                          self.output / 'context' / 'runner.json'):
            with self.subTest(candidate=candidate):
                candidate.parent.mkdir(parents=True, exist_ok=True)
                value = json.loads(original)
                value['context_output'] = str(self.output / 'context')
                encoded = json.dumps(value)
                candidate.write_text(encoded)
                with self.assertRaisesRegex(ValueError, 'runner_path_overlap'):
                    run(candidate, once=True)
                self.assertEqual(candidate.read_text(), encoded)
                self.assertFalse((self.output / '.watch.lock').exists())

    def test_cycle_sanitizes_capture_failure_and_context_backlog(self):
        config = read_config(self.config)
        secret = 'private/path/message-id/key'
        def fail(*_):
            raise RuntimeError(secret)
        failed = _cycle(config, collect=fail, load=lambda _: {'keys': {}}, process=None)
        self.assertEqual(failed['reason'], 'capture_failed')
        self.assertNotIn(secret, json.dumps(failed))
        self.write_config(context_output=str(self.root / 'context'))
        config = read_config(self.config)
        partial = _cycle(config, collect=lambda *_: {'state': 'updated', 'inserted': 2,
                                                    'message_id': secret},
                         load=lambda _: {'keys': {}},
                         process=lambda *_a, **_kw: {'state': 'partial', 'pending': 4,
                                                      'message_id': secret})
        self.assertEqual(partial['state'], 'partial')
        self.assertEqual(partial['reason'], 'context_backlog')
        self.assertEqual(partial['counts'], {'capture': {'inserted': 2},
                                             'context': {'pending': 4}})
        self.assertNotIn(secret, json.dumps(partial))

    def test_once_writes_safe_status_and_releases_lock(self):
        with self.fake_modules(lambda *_: {'state': 'updated', 'inserted': 3,
                                           'message_id': 'sensitive'}), \
             patch('wechat_archive.runner.load_keys', return_value={'keys': {}}):
            result = run(self.config, once=True)
        saved = json.loads((self.output / 'watch-status.json').read_text())
        self.assertEqual(result, saved)
        self.assertEqual(saved['state'], 'complete')
        self.assertEqual(saved['counts'], {'capture': {'inserted': 3}})
        self.assertNotIn('sensitive', json.dumps(saved))
        with CollectorLock(self.output / '.watch.lock'):
            pass

    def test_source_permission_denial_is_explicit_and_sanitized(self):
        def denied(*_args):
            raise PermissionError('private source details')
        result = _cycle(read_config(self.config), collect=denied, load=lambda _: {})
        self.assertEqual(result['state'], 'waiting')
        self.assertEqual(result['reason'], 'source_permission_required')
        self.assertNotIn('private source details', json.dumps(result))

    def test_stop_event_and_existing_lock(self):
        with self.fake_modules(lambda *_: {'state': 'idle'}), \
             patch('wechat_archive.runner.load_keys', return_value={}):
            with CollectorLock(self.output / '.watch.lock') if self.output.exists() else self._prepared_lock():
                with self.assertRaises(AlreadyRunningError):
                    run(self.config, once=True)
            event = threading.Event()
            event.set()
            self.assertEqual(run(self.config, stop_event=event), {})

    def test_retry_wait_is_interruptible_and_backoff_is_bounded(self):
        class StopAfterThreeWaits:
            def __init__(self):
                self.delays = []
            def is_set(self):
                return False
            def wait(self, delay):
                self.delays.append(delay)
                return len(self.delays) == 3
        stop = StopAfterThreeWaits()
        def fail(*_):
            raise RuntimeError('sensitive capture details')
        with self.fake_modules(fail), \
             patch('wechat_archive.runner.load_keys', return_value={}):
            result = run(self.config, stop_event=stop)
        self.assertEqual(result['reason'], 'capture_failed')
        self.assertEqual(stop.delays, [30, 60, 120])
        self.assertNotIn('sensitive', (self.output / 'watch-status.json').read_text())

    def _prepared_lock(self):
        self.output.mkdir()
        return CollectorLock(self.output / '.watch.lock')


if __name__ == '__main__':
    unittest.main()
