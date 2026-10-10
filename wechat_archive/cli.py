"""One cross-platform entry point; sensitive operations remain explicit."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import sys
from .parser import parse_snapshots
from .pipeline import read_key_json, snapshot, databases, validated_keys
from .platform_runtime import CollectorLock
from .security import load_keys, save_keys
from .server import serve
from .storage import private_directory, separated


def counts(stats):
    return {key: stats[key] for key in ['state', 'conversations', 'archived_messages', 'source_rows',
                                        'missing_source_messages', 'contact_count']}


def wizard():
    print('WeChatArchive · 本地工具。首次使用请先阅读 README 中的初始化步骤。')
    path = input('请输入已生成的 archive.sqlite 完整路径：').strip().strip('"')
    serve(path)


def execute(argv=None):
    parser = argparse.ArgumentParser(prog='wechat-archive')
    sub = parser.add_subparsers(dest='command', required=True)
    sub.add_parser('wizard', help='交互打开已有归档')
    p = sub.add_parser('serve', help='分页浏览已有归档')
    p.add_argument('--archive', required=True); p.add_argument('--port', type=int, default=0)
    p = sub.add_parser('parse', help='从已有认证快照解析')
    p.add_argument('--snapshots', required=True); p.add_argument('--output', required=True)
    p = sub.add_parser('archive', help='使用已取得密钥生成全量归档')
    p.add_argument('--source', required=True); p.add_argument('--output', required=True)
    key = p.add_mutually_exclusive_group(required=True)
    key.add_argument('--keys-json'); key.add_argument('--key-store')
    p = sub.add_parser('bootstrap', help='显式初始化本机密钥')
    p.add_argument('--source', required=True); p.add_argument('--state', required=True)
    p.add_argument('--allow-wechat-restart', action='store_true')
    p = sub.add_parser('import-keys', help='验钥后存入本机凭据存储')
    p.add_argument('--source', required=True); p.add_argument('--input', required=True); p.add_argument('--state', required=True)
    p = sub.add_parser('watch', help='持续增量归档并处理上下文')
    p.add_argument('--config', required=True); p.add_argument('--once', action='store_true')
    p = sub.add_parser('process-context', help='逐批整理已有归档上下文')
    p.add_argument('--archive', required=True); p.add_argument('--output', required=True)
    p.add_argument('--batch-size', type=int, default=10000)
    p = sub.add_parser('service-install', help='当前用户登录后自动运行')
    p.add_argument('--config', required=True)
    sub.add_parser('service-uninstall', help='停止自启并保留数据')
    args = parser.parse_args(argv)
    for name in ['archive', 'source', 'output', 'snapshots', 'state', 'input', 'keys_json', 'key_store']:
        if getattr(args, name, None):
            setattr(args, name, str(Path(getattr(args, name)).expanduser().resolve()))
    if args.command in {'service-install', 'service-uninstall'}:
        from .service import install, uninstall
        print(json.dumps(install(args.config) if args.command == 'service-install' else uninstall())); return 0
    if args.command == 'process-context':
        from .context import process_archive
        print(json.dumps(process_archive(args.archive,args.output,batch_size=args.batch_size))); return 0
    if args.command == 'watch':
        import signal, threading
        from .runner import run
        stop = threading.Event()
        old = {}
        for sig in (signal.SIGINT, signal.SIGTERM):
            old[sig] = signal.signal(sig, lambda *_args: stop.set())
        try:
            status = run(args.config,once=args.once,stop_event=stop)
            print(json.dumps(status)); return 0 if status.get('state') in {'complete','partial','degraded'} or stop.is_set() else 1
        finally:
            for sig, handler in old.items(): signal.signal(sig,handler)
    if args.command == 'wizard':
        wizard(); return 0
    if args.command == 'serve':
        serve(args.archive, args.port); return 0
    if args.command in {'bootstrap', 'import-keys'}:
        separated(args.source, args.state)
        state = private_directory(args.state)
        with CollectorLock(state / '.lock'):
            if args.command == 'bootstrap':
                from .bootstrap import mac_keys, windows_keys
                payload = mac_keys(args.source, allow_restart=args.allow_wechat_restart) if sys.platform == 'darwin' else windows_keys(args.source)
            else:
                payload = read_key_json(args.input)
            keys = validated_keys(databases(args.source), payload)
            save_keys(state / 'keys.protected', {'keys': {name: {'enc_key': value.hex()} for name, value in keys.items()}})
        print(json.dumps({'state': 'keys_verified_and_protected', 'database_count': len(keys)})); return 0
    if args.command == 'parse':
        if Path(args.output).resolve().is_relative_to(Path(args.snapshots).resolve()):
            raise ValueError('snapshot_output_overlap')
        manifest = json.loads((Path(args.snapshots) / 'snapshot-status.json').read_text())
        import hashlib, os
        source_id = manifest.get('source_id') or hashlib.sha256(os.fsencode(Path(args.snapshots).resolve())).hexdigest()
        identity = Path(args.output) / 'source-info.json'
        if identity.exists() and json.loads(identity.read_text()).get('source_id') != source_id:
            raise ValueError('output_belongs_to_another_source')
        if not identity.exists() and (Path(args.output) / 'archive.sqlite').exists():
            raise ValueError('existing_archive_without_source_binding')
    else:
        separated(args.source, args.output)
    output = private_directory(args.output)
    with CollectorLock(output / '.lock'):
        if args.command == 'parse':
            stats = parse_snapshots(args.snapshots, output)
        else:
            payload = read_key_json(args.keys_json) if args.keys_json else load_keys(Path(args.key_store))
            stats = snapshot(args.source, output, payload)
    print(json.dumps(counts(stats), ensure_ascii=False)); return 0


def main():
    try:
        return execute()
    except KeyboardInterrupt:
        return 130
    except Exception as error:
        # Never disclose a source path, raw SQL value, key or subprocess output.
        result = {'state': 'failed', 'error_type': type(error).__name__}
        if isinstance(error, ValueError) and str(error) in {
            'temporary_wechat_stop_failed', 'official_wechat_recovery_failed',
            'official_wechat_restart_failed', 'official_signature_changed',
            'captured_key_coverage_incomplete', 'frida_hook_unavailable',
            'explicit_macos_restart_authorization_required', 'official_wechat_must_be_running',
            'official_wechat_did_not_quit', 'source_output_overlap',
            'snapshot_output_overlap', 'output_belongs_to_another_source',
            'archive_recovery_required',
            'continuous_archive_rebuild_requires_new_output',
        }:
            result['reason'] = str(error)
        print(json.dumps(result), file=sys.stderr)
        return 1
