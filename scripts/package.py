"""Create source-based portable packages from an explicitly tracked checkout."""
from __future__ import annotations
import argparse
import hashlib
import json
from pathlib import Path
import subprocess
import sys
import zipfile

ROOT=Path(__file__).resolve().parents[1]
VERSION='0.1.0'
MAC='''#!/bin/bash
set -euo pipefail
ROOT="$(cd "$(dirname "$0")" && pwd)"
python3 -c 'import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)' || { echo '需要 Python 3.11 或更新版本。'; exit 1; }
if [ ! -x "$ROOT/.venv/bin/python" ]; then
  python3 -m venv "$ROOT/.venv"
fi
"$ROOT/.venv/bin/python" -m pip install "$ROOT"/*.whl
exec "$ROOT/.venv/bin/python" -m wechat_archive wizard
'''
WINDOWS='''@echo off
setlocal
cd /d "%~dp0"
py -3 -c "import sys; raise SystemExit(0 if sys.version_info >= (3,11) else 1)"
if errorlevel 1 goto fail
if not exist ".venv\\Scripts\\python.exe" py -3 -m venv .venv
if errorlevel 1 goto fail
for %%f in (*.whl) do ".venv\\Scripts\\python.exe" -m pip install "%%f"
if errorlevel 1 goto fail
".venv\\Scripts\\python.exe" -m wechat_archive wizard
if errorlevel 1 goto fail
exit /b 0
:fail
echo Launch failed. Install Python 3.11+ with the Python launcher, then see README_ZH.md.
pause
exit /b 1
'''


def create(output,platform):
    output=Path(output).resolve();output.mkdir(parents=True,exist_ok=True)
    subprocess.run([sys.executable,'-m','build','--wheel','--no-isolation','--outdir',str(output)],cwd=ROOT,check=True,capture_output=True)
    wheel=output/f'wechat_archive_local-{VERSION}-py3-none-any.whl'
    if not wheel.is_file():raise RuntimeError('wheel_missing')
    platforms=['macos','windows'] if platform=='all' else [platform]
    products=[wheel]
    for target in platforms:
        name=f'WeChatArchive-{VERSION}-{target}-python-portable'
        path=output/(name+'.zip')
        with zipfile.ZipFile(path,'w',zipfile.ZIP_DEFLATED) as archive:
            for file in [wheel,ROOT/'LICENSE',ROOT/'README_ZH.md',ROOT/'THIRD_PARTY.md']:
                archive.write(file,name+'/'+file.name)
            launcher='launch.command' if target=='macos' else 'start.cmd'
            info=zipfile.ZipInfo(name+'/'+launcher);info.external_attr=(0o100755 if target=='macos' else 0o100644)<<16
            archive.writestr(info,MAC if target=='macos' else WINDOWS)
            archive.writestr(name+'/package-kind.json',json.dumps({'version':VERSION,'platform':target,'kind':'python-source-portable','bundled_python':False,'python_minimum':'3.11','native_installer':False,'network_first_dependency_install':True},indent=2))
        products.append(path)
    tracked=subprocess.run(['git','ls-files','-z'],cwd=ROOT,capture_output=True,check=True).stdout.decode().split('\0')
    if not any(tracked):raise RuntimeError('tracked_source_missing')
    source=output/f'WeChatArchive-{VERSION}-source.zip'
    with zipfile.ZipFile(source,'w',zipfile.ZIP_DEFLATED) as archive:
        for name in tracked:
            if name:archive.write(ROOT/name,f'WeChatArchive-{VERSION}-source/'+name)
    products.append(source)
    checks={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in products}
    (output/'SHA256SUMS.json').write_text(json.dumps(checks,indent=2)+'\n')
    print(json.dumps({'artifacts':list(checks),'kind':'python-source-portable'}))


if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--output',required=True);p.add_argument('--platform',choices=['macos','windows','all'],default='all')
    args=p.parse_args();create(args.output,args.platform)
