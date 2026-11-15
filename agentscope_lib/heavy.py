"""Host-wide advisory queue for cooperating heavy commands."""
import fcntl
import json
import math
import os
import stat
import subprocess
import time
import sys


def run(args):
    command = list(args.command)
    if command and command[0] == '--':
        command.pop(0)
    if not command:
        raise ValueError('run requires -- <command>')
    threshold = args.max_load if args.max_load is not None else max(1, os.cpu_count() or 1)
    if not math.isfinite(threshold) or threshold <= 0:
        raise ValueError('--max-load must be positive and finite')
    if not math.isfinite(args.wait_timeout) or args.wait_timeout < 0:
        raise ValueError('--wait-timeout must be nonnegative and finite')
    if args.dry_run:
        print(json.dumps({'command':command,'heavy':args.heavy,'max_load':threshold,'load':os.getloadavg()[0],'execute':False}))
        return 0
    if not args.heavy or os.environ.get('AGENTSCOPE_HEAVY_HELD') == '1':
        return subprocess.run(command).returncode
    path = os.environ.get('AGENTSCOPE_HEAVY_LOCK', '/tmp/agentscope-heavy.lock')
    fd = os.open(path, os.O_RDONLY | os.O_CREAT | os.O_NOFOLLOW, 0o666)
    try:
        if not stat.S_ISREG(os.fstat(fd).st_mode):
            raise ValueError('heavy lock must be a regular file')
        started = time.monotonic()
        next_notice = started
        while True:
            locked = False
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                locked = True
            except BlockingIOError:
                pass
            load = os.getloadavg()[0]
            if locked and load <= threshold:
                env = dict(os.environ, AGENTSCOPE_HEAVY_HELD='1')
                return subprocess.run(command, env=env, pass_fds=(fd,)).returncode
            if time.monotonic() >= next_notice:
                print(f'agentscope: waiting for heavy slot; load={load:.2f}, threshold={threshold}, lock_available={locked}', file=sys.stderr, flush=True)
                next_notice = time.monotonic()+15
            if locked:
                fcntl.flock(fd, fcntl.LOCK_UN)
            if args.wait_timeout and time.monotonic()-started >= args.wait_timeout:
                return 75
            time.sleep(.5)
    finally:
        os.close(fd)
