"""Serialize desktop launches without adding a service or external dependency."""
from contextlib import contextmanager
import os
import time
from .paths import memweave_home


@contextmanager
def startup_lock(timeout=20):
    home=memweave_home()
    home.mkdir(parents=True,exist_ok=True)
    with (home/'runtime-start.lock').open('a+b') as stream:
        stream.seek(0,2)
        if stream.tell()==0:
            stream.write(b'0');stream.flush()
        deadline=time.monotonic()+timeout
        acquired=False
        try:
            while not acquired:
                stream.seek(0)
                try:
                    if os.name=='nt':
                        import msvcrt
                        msvcrt.locking(stream.fileno(),msvcrt.LK_NBLCK,1)
                    else:
                        import fcntl
                        fcntl.flock(stream.fileno(),fcntl.LOCK_EX|fcntl.LOCK_NB)
                    acquired=True
                except OSError:
                    if time.monotonic()>=deadline:
                        raise RuntimeError('另一个 MemWeave 入口正在启动，请稍后重试') from None
                    time.sleep(.1)
            yield
        finally:
            if acquired:
                stream.seek(0)
                if os.name=='nt':
                    import msvcrt
                    msvcrt.locking(stream.fileno(),msvcrt.LK_UNLCK,1)
                else:
                    import fcntl
                    fcntl.flock(stream.fileno(),fcntl.LOCK_UN)
