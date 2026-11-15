import argparse
import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import sys
import time
import types
from unittest.mock import patch
import unittest
import test_extensions
from agentscope_lib import cleanup, disk, extensions, heavy


class R2Tests(unittest.TestCase):
    setUp = test_extensions.ExtensionTests.setUp
    def test_disk_nonproject_build_is_display_only(self):
        target=self.root/'tax/build'
        target.mkdir(parents=True)
        row=next(r for r in disk.collect()['items'] if r['path']==str(target))
        self.assertFalse(row['deletable'])
        self.assertIsNone(row['suggestion'])
        self.assertFalse(disk.clean(str(target),yes=True))
        (target.parent/'.git').mkdir()
        self.assertFalse(disk.clean(str(target),yes=True))
        (target.parent/'package.json').write_text('{}')
        self.assertEqual(disk.deletion_evidence(str(target),[str(self.root)]),str(target.parent))
        with patch.object(cleanup,'analyze',return_value=({},[])):
            self.assertTrue(disk.clean(str(target),yes=True))

    def test_disk_project_markers(self):
        repo=self.root/'code'
        repo.mkdir()
        (repo/'.git').mkdir()
        for name,marker in [('.venv','requirements-dev.txt'),('target','Cargo.toml'),('build','build.gradle.kts')]:
            target=repo/name
            target.mkdir()
            self.assertIsNone(disk.deletion_evidence(str(target),[str(self.root)]))
            (repo/marker).write_text('')
            self.assertEqual(disk.deletion_evidence(str(target),[str(self.root)]),str(repo))
            (repo/marker).unlink()

    def test_heavy_default_timeout_and_nested(self):
        parser=argparse.ArgumentParser()
        extensions.register(parser.add_subparsers())
        args=parser.parse_args(['run','--heavy','--','test'])
        self.assertEqual(args.wait_timeout,300)
        with patch.dict(os.environ,AGENTSCOPE_HEAVY_HELD='1'),patch.object(heavy.os,'open') as opened,patch.object(heavy.subprocess,'run',return_value=types.SimpleNamespace(returncode=0)):
            self.assertEqual(heavy.run(args),0)
            opened.assert_not_called()

    def test_child_keeps_lock_after_wrapper_exit(self):
        ready=self.root/'ready'
        done=self.root/'done'
        child="import pathlib,time; pathlib.Path(%r).write_text('ready'); time.sleep(2); pathlib.Path(%r).write_text('done')"%(str(ready),str(done))
        wrapper='''import os,sys,time,threading,types
from agentscope_lib.heavy import run
def leave():
    while not os.path.exists(sys.argv[1]): time.sleep(.01)
    os._exit(0)
threading.Thread(target=leave,daemon=True).start()
run(types.SimpleNamespace(command=[sys.executable,'-c',sys.argv[2]],heavy=True,max_load=100000,wait_timeout=5,dry_run=False))
'''
        parent=subprocess.Popen([sys.executable,'-c',wrapper,str(ready),child])
        parent.wait(timeout=8)
        self.assertTrue(ready.exists())
        fd=os.open(os.environ['AGENTSCOPE_HEAVY_LOCK'],os.O_RDONLY)
        try:
            with self.assertRaises(BlockingIOError):
                fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
            deadline=time.monotonic()+5
            while time.monotonic()<deadline:
                try:
                    fcntl.flock(fd,fcntl.LOCK_EX|fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    time.sleep(.05)
            else:
                self.fail('child failed to release lock on exit')
            self.assertTrue(done.exists())
        finally:
            os.close(fd)

    def test_timeline_invalid_is_friendly(self):
        with contextlib.redirect_stderr(io.StringIO()) as err:
            self.assertEqual(extensions.cmd_timeline(types.SimpleNamespace(since='bad',json=True)),2)
        self.assertIn('Invalid --since',err.getvalue())
