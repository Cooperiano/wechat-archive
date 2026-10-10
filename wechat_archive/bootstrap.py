"""Explicit native key acquisition; no source signing or secret console output."""
from __future__ import annotations
import hashlib
import os
import signal
import shutil
from pathlib import Path
import plistlib
import queue
import re
import subprocess
import sys
import tempfile
import time
from .cipher import verify_enc_key
from .pipeline import databases, validated_keys


def run(command, timeout=30):
    return subprocess.run(command, capture_output=True, check=True, timeout=timeout)


def app_identity(application):
    run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(application)], 90)
    result = run(['/usr/bin/codesign', '-dv', '--verbose=4', str(application)])
    fields = dict(re.findall(r'^(Identifier|TeamIdentifier|CDHash)=(.+)$', result.stderr.decode(), re.M))
    with (application / 'Contents/Info.plist').open('rb') as stream:
        info = plistlib.load(stream)
    if fields.get('Identifier') != 'com.tencent.xinWeChat' or fields.get('TeamIdentifier') != '5A4RE8SF68':
        raise ValueError('official_wechat_identity_unexpected')
    if info.get('CFBundleExecutable') != 'WeChat' or not fields.get('CDHash'):
        raise ValueError('official_wechat_identity_unexpected')
    return fields


def running_binary(binary):
    result = run(['/bin/ps', '-axo', 'pid=,comm='])
    for line in result.stdout.decode().splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[1] == str(binary):
            return True
    return False


def copy_processes(copy):
    result = run(['/bin/ps', '-axo', 'pid=,comm='])
    owned = (copy / 'Contents').resolve()
    found = []
    for line in result.stdout.decode().splitlines():
        parts = line.strip().split(None, 1)
        if len(parts) == 2 and parts[0].isdigit() and Path(parts[1]).resolve().is_relative_to(owned):
            found.append(int(parts[0]))
    return found


def stop_copy(copy):
    for sig in (signal.SIGTERM, signal.SIGKILL):
        for process in copy_processes(copy):
            # Re-check ownership immediately before signaling each PID.
            if process in copy_processes(copy):
                try:
                    os.kill(process, sig)
                except ProcessLookupError:
                    pass
        deadline = time.monotonic() + 5
        while copy_processes(copy) and time.monotonic() < deadline:
            time.sleep(0.1)
        if not copy_processes(copy):
            return
    raise ValueError('temporary_wechat_stop_failed')


def mac_keys(source, *, allow_restart=False, timeout=120):
    if sys.platform != 'darwin' or not allow_restart:
        raise ValueError('explicit_macos_restart_authorization_required')
    import frida
    records = databases(source)
    official = Path('/Applications/WeChat.app')
    identity = app_identity(official)
    official_binary = official / 'Contents/MacOS/WeChat'
    if not running_binary(official_binary):
        raise ValueError('official_wechat_must_be_running')
    messages = queue.Queue(maxsize=4096)
    device, session, pid = None, None, None
    interrupted = False
    captured = {}
    recovery_error = None
    temporary = str(Path(tempfile.mkdtemp(prefix='wechat-archive-bootstrap-')).resolve())
    try:
        copy = Path(temporary) / 'WeChat.app'
        run(['/usr/bin/ditto', str(official), str(copy)], 300)
        if not copy.resolve().is_relative_to(Path(temporary).resolve()):
            raise ValueError('unsafe_copy_location')
        run(['/usr/bin/codesign', '--force', '--deep', '--sign', '-', str(copy)], 180)
        try:
            interrupted = True
            run(['/usr/bin/osascript', '-e', 'tell application id "com.tencent.xinWeChat" to quit'])
            deadline = time.monotonic() + 20
            while running_binary(official_binary) and time.monotonic() < deadline:
                time.sleep(0.25)
            if running_binary(official_binary):
                raise ValueError('official_wechat_did_not_quit')
            device = frida.get_local_device()
            pid = device.spawn([str(copy / 'Contents/MacOS/WeChat')])
            session = device.attach(pid)
            script = session.create_script((Path(__file__).parent / 'vendor/hook_pbkdf.js').read_text())
            def receive(message, _data):
                if message.get('type') == 'send':
                    try:
                        messages.put_nowait(message.get('payload', {}))
                    except queue.Full:
                        pass
            script.on('message', receive)
            script.load()
            device.resume(pid)
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline and len(captured) < len(records):
                try:
                    candidate = messages.get(timeout=0.25)
                except queue.Empty:
                    continue
                if candidate.get('error'):
                    raise ValueError('frida_hook_unavailable')
                if candidate.get('kind') not in {'raw', 'derived'} or not re.fullmatch('[0-9a-f]{64}', candidate.get('hex', '')):
                    continue
                material = bytes.fromhex(candidate['hex'])
                for name, _, page in records:
                    if name in captured:
                        continue
                    key = hashlib.pbkdf2_hmac('sha512', material, page[:16], 256000, 32) if candidate['kind'] == 'raw' else material
                    if verify_enc_key(key, page):
                        captured[name] = {'enc_key': key.hex()}
            if len(captured) != len(records):
                raise ValueError('captured_key_coverage_incomplete')
        finally:
            try:
                stop_copy(copy)
            except Exception:
                recovery_error = 'temporary_wechat_stop_failed'
            if session is not None:
                try:
                    session.detach()
                except Exception:
                    # A detached session must not skip official-app recovery.
                    pass
            if interrupted:
                try:
                    if app_identity(official) != identity:
                        raise ValueError('official_signature_changed')
                    if recovery_error is None:
                        run(['/usr/bin/open', '-a', str(official)])
                        deadline = time.monotonic() + 20
                        while not running_binary(official_binary) and time.monotonic() < deadline:
                            time.sleep(0.25)
                        if not running_binary(official_binary):
                            raise ValueError('official_wechat_restart_failed')
                except Exception:
                    recovery_error = 'official_wechat_recovery_failed'
            if recovery_error:
                raise ValueError(recovery_error)
    finally:
        # Retain the staging directory if an owned process could not stop.
        try:
            safe_to_remove = not copy_processes(copy)
        except Exception:
            safe_to_remove = False
        if safe_to_remove:
            shutil.rmtree(temporary)
    return {'keys': captured}


def windows_keys(source):
    if sys.platform != 'win32':
        raise ValueError('windows_bootstrap_requires_windows')
    from .vendor import wcdb_windows as tool
    records = databases(source)
    tool._print = lambda *_args, **_kwargs: None
    tool._save_results = lambda *_args, **_kwargs: None
    tool.logger.disabled = True
    # Keep the scanner's enumeration inside the validated account boundary.
    def collect(_source):
        rows = [(name, str(path), path.stat().st_size, page[:16].hex(), page) for name, path, page in records]
        salts = {}
        for name, _, _, salt, _ in rows:
            salts.setdefault(salt, []).append(name)
        return rows, salts
    tool.collect_db_files = collect
    payload = {'keys_by_salt': tool._scan_memory_raw_key(str(source), 'unused')}
    validated_keys(records, payload)
    return payload
