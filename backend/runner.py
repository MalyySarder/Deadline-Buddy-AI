"""Single local instance with bounded restart delay and metadata-only logs."""
import asyncio
import logging
import os
import time
from pathlib import Path
from telegram_bot import run

RUNTIME=Path(__file__).resolve().parent.parent/'.runtime'
RUNTIME.mkdir(exist_ok=True)
logging.disable(logging.CRITICAL)

def main():
    with (RUNTIME/'bot.lock').open('a+b') as lock:
        lock.seek(0)
        if os.name=='nt':
            import msvcrt
            try:
                msvcrt.locking(lock.fileno(),msvcrt.LK_NBLCK,1)
            except OSError:
                print('Another bot instance is already running',flush=True)
                return 1
        else:
            import fcntl
            try:
                fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
            except OSError:
                return 1
        (RUNTIME/'bot.pid').write_text(str(os.getpid()),encoding='ascii')
        delay=5
        while True:
            try:
                result=asyncio.run(run())
                if result==1:
                    return 1
            except KeyboardInterrupt:
                return 0
            except Exception as exc:
                print('Restart after '+type(exc).__name__+'; details omitted',flush=True)
            time.sleep(delay)
            delay=min(delay*2,60)

if __name__=='__main__':
    raise SystemExit(main())
