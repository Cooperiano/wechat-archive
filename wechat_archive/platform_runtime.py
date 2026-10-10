"""Small platform operations shared by the Windows and macOS collectors."""
from __future__ import annotations
import errno
import os
import stat
from pathlib import Path
import sys


class AlreadyRunningError(RuntimeError):
    pass


class CollectorLock:
    """One writer per private directory, including across login sessions."""
    def __init__(self, path: Path):
        path = Path(path)
        if path.is_symlink():
            raise ValueError('lock_symlink_rejected')
        descriptor = os.open(path, os.O_RDWR | os.O_CREAT | getattr(os, 'O_NOFOLLOW', 0), 0o600)
        info = os.fstat(descriptor)
        if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1 or (os.name == 'posix' and info.st_uid != os.getuid()):
            os.close(descriptor)
            raise ValueError('unsafe_lock_file')
        self.file = os.fdopen(descriptor, 'r+b')
        try:
            if sys.platform == 'win32':
                import msvcrt
                if self.file.seek(0, os.SEEK_END) == 0:
                    self.file.write(b'\0')
                    self.file.flush()
                self.file.seek(0)
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                os.fchmod(self.file.fileno(), 0o600)
                fcntl.flock(self.file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError as exc:
            self.file.close()
            if exc.errno in (errno.EACCES, errno.EAGAIN, errno.EDEADLK):
                raise AlreadyRunningError('collector already running') from None
            raise
        except BaseException:
            self.file.close()
            raise

    def close(self):
        self.file.close()

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        self.close()
