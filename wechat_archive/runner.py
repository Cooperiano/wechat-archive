"""Local, single-writer polling of an already initialized archive."""
from __future__ import annotations

import json
import os
from pathlib import Path
import threading
from datetime import datetime, timezone
from dataclasses import dataclass

from .platform_runtime import CollectorLock
from .security import load_keys
from .storage import private_directory


_ALLOWED_CONFIG = frozenset({'source', 'output', 'key_store', 'context_output',
                             'interval_seconds', 'context_batch_size'})
_RESERVED_OUTPUT = frozenset({'snapshots', 'archive.sqlite', 'source-info.json',
                              'status.json', 'watch-status.json', '.lock', '.watch.lock'})
_CAPTURE_COUNTS = frozenset({'conversations', 'archived_messages', 'inserted',
                             'updated', 'deleted', 'changed_databases', 'revision'})
_CONTEXT_COUNTS = frozenset({'conversations', 'indexed', 'updated', 'deleted',
                             'processed', 'pending', 'indexed_messages', 'days'})


@dataclass(frozen=True)
class RunnerConfig:
    source: Path
    output: Path
    key_store: Path
    context_output: Path | None
    interval_seconds: int
    context_batch_size: int


def _resolved(value: object, field: str) -> Path:
    if not isinstance(value, str) or not value.strip() or '\x00' in value:
        raise ValueError(f'invalid_{field}')
    path = Path(value).expanduser()
    if not path.is_absolute():
        raise ValueError(f'invalid_{field}')
    if path.is_symlink():
        raise ValueError(f'invalid_{field}')
    return path.resolve()


def _overlap(left: Path, right: Path) -> bool:
    return left.is_relative_to(right) or right.is_relative_to(left)


def read_config(config_path: str | Path) -> RunnerConfig:
    path = Path(config_path).expanduser()
    if path.is_symlink() or not path.is_file() or path.stat().st_size > 64 * 1024:
        raise ValueError('invalid_runner_config')
    path = path.resolve()
    try:
        raw = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError('invalid_runner_config') from None
    if not isinstance(raw, dict) or set(raw) - _ALLOWED_CONFIG:
        raise ValueError('invalid_runner_config')
    for name in ('source', 'output', 'key_store'):
        if name not in raw:
            raise ValueError(f'missing_{name}')
    source = _resolved(raw['source'], 'source')
    output = _resolved(raw['output'], 'output')
    key_store = _resolved(raw['key_store'], 'key_store')
    context_output = (_resolved(raw['context_output'], 'context_output')
                      if 'context_output' in raw else None)
    interval = raw.get('interval_seconds', 120)
    if type(interval) is not int or interval < 30 or interval > 86_400:
        raise ValueError('invalid_interval_seconds')
    batch_size = raw.get('context_batch_size', 10_000)
    if type(batch_size) is not int or not 1 <= batch_size <= 10_000:
        raise ValueError('invalid_context_batch_size')
    if _overlap(source, output) or _overlap(source, key_store) or _overlap(output, key_store):
        raise ValueError('runner_path_overlap')
    if context_output:
        if context_output == output or any(_overlap(context_output, other)
                                           for other in (source, key_store)):
            raise ValueError('runner_path_overlap')
        if not context_output.is_relative_to(output) and _overlap(context_output, output):
            raise ValueError('runner_path_overlap')
        if context_output.is_relative_to(output) and context_output.relative_to(output).parts[0] in _RESERVED_OUTPUT:
            raise ValueError('runner_path_overlap')
    if path.is_relative_to(source) or (context_output and path.is_relative_to(context_output)):
        raise ValueError('runner_path_overlap')
    if path == output or (path.is_relative_to(output)
                          and path.relative_to(output).parts[0] in _RESERVED_OUTPUT):
        raise ValueError('runner_path_overlap')
    if output.exists() and not output.is_dir():
        raise ValueError('invalid_output')
    if context_output and context_output.exists() and not context_output.is_dir():
        raise ValueError('invalid_context_output')
    if source.exists() and not source.is_dir():
        raise ValueError('invalid_source')
    if key_store.exists() and not key_store.is_file():
        raise ValueError('invalid_key_store')
    return RunnerConfig(source, output, key_store, context_output, interval, batch_size)


def _utc() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')


def _safe_counts(result: object, allowed: frozenset[str]) -> dict[str, int]:
    counts: dict[str, int] = {}
    if not isinstance(result, dict):
        return counts
    for key, value in result.items():
        if key in allowed and type(value) is int and value >= 0:
            counts[key] = value
    return counts


def _context_partial(result: object) -> bool:
    if not isinstance(result, dict):
        return False
    if result.get('state') in {'partial', 'pending', 'backlog'}:
        return True
    return (type(result.get('pending')) is int and result['pending'] > 0
            or result.get('baseline_complete') is False)


def _write_status(output: Path, status: dict) -> None:
    target = output / 'watch-status.json'
    if target.is_symlink():
        raise ValueError('unsafe_status_file')
    temp = output / f'.watch-status-{os.getpid()}-{threading.get_ident()}.tmp'
    descriptor = os.open(temp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, 'O_NOFOLLOW', 0), 0o600)
    try:
        with os.fdopen(descriptor, 'w', encoding='utf-8') as writer:
            json.dump(status, writer, ensure_ascii=False, separators=(',', ':'))
            writer.flush()
            os.fsync(writer.fileno())
        os.replace(temp, target)
    finally:
        temp.unlink(missing_ok=True)


def _cycle(config: RunnerConfig, *, collect, load, process=None) -> dict:
    """Run one serial tick. Results are reduced to an explicit public-safe schema."""
    started = _utc()
    if not config.source.is_dir():
        return {'state': 'waiting', 'reason': 'source_unavailable', 'updated_at': started, 'counts': {}}
    if not config.key_store.is_file():
        return {'state': 'waiting', 'reason': 'key_unavailable', 'updated_at': started, 'counts': {}}
    try:
        payload = load(config.key_store)
    except Exception:
        return {'state': 'waiting', 'reason': 'key_unavailable', 'updated_at': started, 'counts': {}}
    try:
        collected = collect(config.source, config.output, payload)
    except Exception:
        return {'state': 'retrying', 'reason': 'capture_failed', 'updated_at': started, 'counts': {}}
    if not isinstance(collected, dict) or collected.get('state') not in {'updated', 'idle'}:
        return {'state': 'retrying', 'reason': 'capture_failed', 'updated_at': started, 'counts': {}}
    counts = {'capture': _safe_counts(collected, _CAPTURE_COUNTS)}
    if config.context_output is not None:
        try:
            processed = process(config.output / 'archive.sqlite', config.context_output,
                                batch_size=config.context_batch_size)
        except Exception:
            return {'state': 'partial', 'reason': 'context_failed', 'updated_at': started, 'counts': counts}
        if not isinstance(processed, dict):
            return {'state': 'partial', 'reason': 'context_failed', 'updated_at': started, 'counts': counts}
        counts['context'] = _safe_counts(processed, _CONTEXT_COUNTS)
        if _context_partial(processed):
            return {'state': 'partial', 'reason': 'context_backlog', 'updated_at': started, 'counts': counts}
        if processed.get('state') != 'complete':
            return {'state': 'partial', 'reason': 'context_degraded', 'updated_at': started, 'counts': counts}
    return {'state': 'complete', 'reason': 'none', 'updated_at': started, 'counts': counts}


def run(config_path: str | Path, *, once: bool = False, stop_event=None) -> dict:
    """Poll until stopped. Missing inputs and capture failures retry with bounded backoff.

    This function never initializes credentials, modifies WeChat, or starts a service.
    """
    config = read_config(config_path)  # Entire path boundary is checked before a write.
    output = private_directory(config.output)
    stop = stop_event if stop_event is not None else threading.Event()
    from .incremental import collect
    if config.context_output is not None:
        from .context import process_archive
    else:
        process_archive = None
    with CollectorLock(output / '.watch.lock'):
        failures = 0
        last_success_at = None
        status: dict = {}
        while not stop.is_set():
            status = _cycle(config, collect=collect, load=load_keys, process=process_archive)
            if status['state'] == 'complete':
                failures = 0
                last_success_at = status['updated_at']
            elif status['state'] in {'waiting', 'retrying'} or status['reason'] == 'context_failed':
                failures += 1
            else:
                failures = 0
            status['last_success_at'] = last_success_at
            status['failure_count'] = failures
            _write_status(output, status)
            if once:
                break
            if status['reason'] == 'context_backlog':
                delay = 5
            elif failures:
                delay = min(config.interval_seconds * (2 ** min(failures - 1, 5)), 900)
            else:
                delay = config.interval_seconds
            if stop.wait(delay):
                break
        return status
