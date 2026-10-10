"""Private output preparation and source/manifest boundary checks."""
from pathlib import Path, PurePosixPath
import base64
import os
import subprocess
import sys
import stat


def safe_sqlite_files(path):
    """Reject aliased or foreign database files before SQLite can touch sidecars."""
    path = Path(path)
    for file in (path, *(Path(str(path) + suffix) for suffix in ('-wal', '-shm', '-journal'))):
        try:
            info = file.lstat()
        except FileNotFoundError:
            continue
        if (not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or
                (os.name == 'posix' and info.st_uid != os.getuid())):
            raise ValueError('unsafe_archive_file')


def private_directory(path):
    path = Path(path).expanduser().absolute()
    if any(part.is_symlink() for part in [path, *path.parents]):
        raise ValueError('private_directory_symlink')
    path.mkdir(parents=True, exist_ok=True, mode=0o700)
    if os.name == 'posix':
        if path.stat().st_uid != os.getuid():
            raise ValueError('private_directory_not_owned')
        path.chmod(0o700)
    elif sys.platform == 'win32':
        user = subprocess.run(['powershell.exe', '-NoProfile', '-Command',
                               '[System.Security.Principal.WindowsIdentity]::GetCurrent().User.Value'],
                              capture_output=True, timeout=15, check=True).stdout.decode().strip()
        import re
        if not re.fullmatch(r'S-1-(?:\d+-)+\d+', user):
            raise ValueError('windows_identity_unavailable')
        # Replace the complete DACL, including explicit sharing rules. Then
        # reject any descendant retaining an explicit grant to another SID.
        script = '''$ErrorActionPreference='Stop'
if(((Get-Item -LiteralPath $env:WECHAT_ARCHIVE_DIRECTORY).Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0){throw 'unsafe_reparse_point'}
$sid=[System.Security.Principal.SecurityIdentifier]::new($env:WECHAT_ARCHIVE_SID)
$acl=[System.Security.AccessControl.DirectorySecurity]::new()
$acl.SetAccessRuleProtection($true,$false)
$rule=[System.Security.AccessControl.FileSystemAccessRule]::new($sid,'FullControl','ContainerInherit,ObjectInherit','None','Allow')
$acl.AddAccessRule($rule)
[System.IO.Directory]::SetAccessControl($env:WECHAT_ARCHIVE_DIRECTORY,$acl)
$items=@(Get-Item -LiteralPath $env:WECHAT_ARCHIVE_DIRECTORY)+@(Get-ChildItem -LiteralPath $env:WECHAT_ARCHIVE_DIRECTORY -Force -Recurse)
foreach($item in $items){
 if(($item.Attributes -band [System.IO.FileAttributes]::ReparsePoint) -ne 0){throw 'unsafe_reparse_point'}
 if($item.PSIsContainer){$read=[System.IO.Directory]::GetAccessControl($item.FullName)}else{$read=[System.IO.File]::GetAccessControl($item.FullName)}
 $rules=$read.GetAccessRules($true,$true,[System.Security.Principal.SecurityIdentifier])
 foreach($entry in $rules){if($entry.AccessControlType -eq 'Allow' -and $entry.IdentityReference.Value -ne $sid.Value){throw 'unsafe_existing_access_rule'}}
}
'''
        environment = dict(os.environ, WECHAT_ARCHIVE_DIRECTORY=str(path), WECHAT_ARCHIVE_SID=user)
        subprocess.run(['powershell.exe', '-NoProfile', '-EncodedCommand',
                        base64.b64encode(script.encode('utf-16-le')).decode('ascii')],
                       env=environment, capture_output=True, timeout=60, check=True)
    return path.resolve()


def relative_database(root, name):
    root = Path(root).expanduser().resolve()
    rel = PurePosixPath(str(name).replace('\\', '/'))
    if rel.is_absolute() or '..' in rel.parts or ':' in str(rel) or not rel.parts:
        raise ValueError('unsafe_database_path')
    path = root.joinpath(*rel.parts)
    if any(item.is_symlink() for item in [path, *path.parents] if item != root and root in item.parents):
        raise ValueError('database_symlink')
    if not path.resolve().is_relative_to(root):
        raise ValueError('database_outside_source')
    return path


def separated(source, output):
    source, output = Path(source).expanduser().resolve(), Path(output).expanduser().resolve()
    if source.is_relative_to(output) or output.is_relative_to(source):
        raise ValueError('source_output_overlap')
