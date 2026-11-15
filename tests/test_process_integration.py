"""Real signals are restricted to identities created by this test, never existing processes."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch
from agentscope_lib import cleanup, config, history, procstat


@unittest.skipUnless(sys.platform == 'darwin', 'macOS libproc integration')
class OwnedProcessTests(unittest.TestCase):
    def test_live_parent_then_double_fork_orphan(self):
        with tempfile.TemporaryDirectory() as directory:
            cfgpath = Path(directory)/'config.json'
            cfgpath.write_text(json.dumps({'workspaces':[directory]}))
            with patch.dict(os.environ, AGENTSCOPE_CONFIG=str(cfgpath), AGENTSCOPE_WORKSPACES='', AGENTSCOPE_STATE_DIR=directory+'/state'):
                cfg = config.load_config()
                owned = {}
                pgids = set()
                real_kill = os.kill
                def remember(info):
                    for pid, b in info.items():
                        if b['pgid'] in pgids:
                            owned[pid] = b['start_time']
                def guarded_signal(pid, sig):
                    if sig != 0:
                        self.assertIn(pid, owned)
                        current = procstat.proc_bsdinfo(pid)
                        self.assertTrue(current and current['start_time'] == owned[pid])
                    return real_kill(pid, sig)
                launcher = None
                live = subprocess.Popen(['/bin/sh', '-c', 'sleep 30 & sleep 30'], cwd=directory, start_new_session=True)
                pgids.add(live.pid)
                try:
                    deadline = time.monotonic()+5
                    while time.monotonic() < deadline:
                        info = procstat.scan_processes()
                        remember(info)
                        members = [p for p,b in info.items() if b['pgid'] == live.pid]
                        if len(members) >= 2:
                            break
                        time.sleep(.05)
                    self.assertGreaterEqual(len(members), 2)
                    cwds = procstat.load_cwds(members)
                    group = cleanup.classify_group(live.pid, members, info, cwd_query=lambda p:cwds.get(p, ""),
                        ports_query=lambda:{}, tty_query=lambda:set(), app_query=lambda p:set(), cfg=cfg)
                    self.assertFalse(group['orphan'])
                    pidfile = str(Path(directory)/'orphan.pid')
                    source = '''import os, sys
p = os.fork()
if p:
    os.waitpid(p, 0)
    sys.exit(0)
os.setsid()
p = os.fork()
if p:
    os._exit(0)
with open(sys.argv[1], 'w') as f:
    f.write(str(os.getpid()) + ' ' + str(os.getpgrp()))
os.execl('/bin/sh', 'sh', '-c', 'sleep 30 & wait')
'''
                    launcher = subprocess.Popen([sys.executable, '-c', source, pidfile], cwd=directory)
                    launcher.wait(timeout=5)
                    deadline = time.monotonic()+5
                    while not Path(pidfile).exists() and time.monotonic()<deadline:
                        time.sleep(.05)
                    root, pgid = map(int, Path(pidfile).read_text().split())
                    pgids.add(pgid)
                    while time.monotonic()<deadline:
                        info = procstat.scan_processes()
                        remember(info)
                        members = [p for p,b in info.items() if b['pgid']==pgid]
                        if len(members)>=2 and info[root]['ppid']==1 and info[root]['comm']=='sh':
                            break
                        time.sleep(.05)
                    self.assertGreaterEqual(len(members),2)
                    group = cleanup.classify_group(pgid,members,info,cwd_query=lambda p:directory,
                        ports_query=lambda:{},tty_query=lambda:set(),app_query=lambda p:set(),cfg=cfg)
                    self.assertTrue(group['orphan'])
                    with patch.object(cleanup.os, 'kill', side_effect=guarded_signal) as calls:
                        self.assertTrue(cleanup.kill_group(pgid, expected=group, timeout=2, deadline=time.monotonic()+20))
                    term = [c.args[0] for c in calls.call_args_list if c.args[1]==signal.SIGTERM]
                    self.assertEqual(term[-1],root)
                    self.assertEqual(set(term),set(members))
                    self.assertEqual(history.read(['kills'])[-1]['result'],'cleaned')
                finally:
                    # Discover late-born children only in groups created by this test.
                    remember(procstat.scan_processes())
                    for pid, start in owned.items():
                        current = procstat.proc_bsdinfo(pid)
                        if current and current['start_time']==start and current['status']!=5:
                            try:
                                real_kill(pid,signal.SIGKILL)
                            except ProcessLookupError:
                                pass
                    live.wait(timeout=5)
                    if launcher and launcher.poll() is None:
                        launcher.kill()
                        launcher.wait(timeout=5)
