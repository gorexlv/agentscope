import contextlib
import fcntl
import io
import json
import os
from pathlib import Path
import subprocess
import tempfile
import time
import types
import unittest
from unittest.mock import patch
from agentscope_lib import activity, agents, cleanup, config, disk, doctor, heavy, history, mcp, metrics, ownership, ports
from test_hardening import process


class ExtensionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve()
        self.config_path = self.root/'config.json'
        self.config_path.write_text(json.dumps({'workspaces':[str(self.root)]}))
        env = patch.dict(os.environ, AGENTSCOPE_CONFIG=str(self.config_path), AGENTSCOPE_WORKSPACES='',
                         AGENTSCOPE_STATE_DIR=str(self.root/'state'),AGENTSCOPE_HEAVY_LOCK=str(self.root/'heavy.lock'))
        env.start()
        self.addCleanup(env.stop)

    def test_13_mcp_duplicates_memory_and_ancestor(self):
        info = {10:process(10,1,'codex'), 11:process(11,10),12:process(12,10)}
        info[11]['cmdline']='node /tmp/mcp-server-filesystem'
        info[12]['cmdline']='npx @modelcontextprotocol/server-filesystem'
        with patch.object(mcp.procstat,'task_resources',return_value={'rss_bytes':1024**2,'cpu_ticks':1}):
            rows=mcp.collect(info,{11:'/work',12:'/work'},[{'pid':10,'cwd':'/work'}])
        self.assertEqual(len(rows),1)
        self.assertEqual(rows[0]['count'],2)
        self.assertEqual(rows[0]['mem_mb'],2)
        self.assertEqual(rows[0]['instances'][0]['session_pids'],[10])
        self.assertEqual(rows[0]['instances'][0]['attribution'],'ancestor')
        self.assertIsNone(mcp.server_name({'cmdline':'node regular.js'}))

    def test_13_mcp_ambiguous_cwd(self):
        info={11:dict(process(11,1),cmdline='foo-mcp')}
        rows=mcp.collect(info,{11:'/shared'},[{'pid':1,'cwd':'/shared'},{'pid':2,'cwd':'/shared'}],False)
        self.assertEqual(rows[0]['instances'][0]['attribution'],'cwd-ambiguous')
        self.assertFalse(rows[0]['memory_complete'])

    def test_14_abandoned_ports_explicit_only_and_revalidated(self):
        cwd=str(self.root/'deleted')
        info={100:process(100,1,'sh'),101:process(101,100)}
        g=cleanup.classify_group(100,[100,101],info,cwd_query=lambda p:cwd,ports_query=lambda:{101:['192.168.1.2:3000']},tty_query=lambda:set(),app_query=lambda p:set())
        self.assertTrue(g['abandoned_server'])
        with patch.object(cleanup,'my_protected_pids',return_value=set()):
            self.assertFalse(cleanup.cleanable(g,info))
            self.assertTrue(cleanup.cleanable(g,info,allow_abandoned=True))
            Path(cwd).mkdir()
            fresh=cleanup.classify_group(100,[100,101],info,cwd_query=lambda p:cwd,ports_query=lambda:{101:['*:3000']},tty_query=lambda:set(),app_query=lambda p:set())
            self.assertFalse(cleanup.cleanable(fresh,info,allow_abandoned=True))
        data=ports.collect(info,{101:str(self.root/'gone')},[],{101:['*:3000']})
        self.assertTrue(data['services'][0]['abandoned'])
        with patch.object(ports.os if hasattr(ports,'os') else ownership.os,'stat',side_effect=PermissionError):
            self.assertFalse(ownership.missing_path(cwd))

    def test_15_activity_states(self):
        path=self.root/'session.jsonl'
        cases=[('permission_request',0,0,'WAITING_PERMISSION'),('task_complete',0,0,'WAITING_INPUT'),
               ('user',0,0,'WORKING'),('function_call',700,30,'SUSPECTED_LOOP'),
               ('function_call',700,0,'SUSPECTED_STALL'),('other',200,0,'IDLE')]
        now=time.time()
        for kind,age,cpu,state in cases:
            path.write_text(json.dumps({'payload':{'type':kind}})+'\n')
            os.utime(path,(now-age,now-age))
            self.assertEqual(activity.state(str(path),cpu,now)['activity_state'],state)
        self.assertEqual(activity.state(None)['activity_state'],'UNKNOWN')

    def test_16_heavy_queue_waits_for_lock_and_preserves_exit(self):
        args=types.SimpleNamespace(command=['--','test-command'],heavy=True,max_load=1,wait_timeout=2,dry_run=False)
        fd=os.open(os.environ['AGENTSCOPE_HEAVY_LOCK'],os.O_CREAT|os.O_RDWR,0o600)
        fcntl.flock(fd,fcntl.LOCK_EX)
        def release(_):
            fcntl.flock(fd,fcntl.LOCK_UN)
        try:
            with patch.object(heavy.os,'getloadavg',return_value=(0,0,0)), patch.object(heavy.time,'sleep',side_effect=release) as wait, patch.object(heavy.subprocess,'run',return_value=types.SimpleNamespace(returncode=7)) as execute:
                self.assertEqual(heavy.run(args),7)
                wait.assert_called_once()
                self.assertEqual(execute.call_args.args[0],['test-command'])
                self.assertEqual(execute.call_args.kwargs['env']['AGENTSCOPE_HEAVY_HELD'],'1')
                self.assertTrue(execute.call_args.kwargs['pass_fds'])
        finally:
            os.close(fd)

    def test_16_load_gate_and_dry_run(self):
        args=types.SimpleNamespace(command=['--','test-command'],heavy=True,max_load=1,wait_timeout=2,dry_run=False)
        with patch.object(heavy.os,'getloadavg',side_effect=[(3,0,0),(0,0,0)]),patch.object(heavy.time,'sleep') as wait,patch.object(heavy.subprocess,'run',return_value=types.SimpleNamespace(returncode=0)):
            self.assertEqual(heavy.run(args),0)
            wait.assert_called_once()
        args.dry_run=True
        with patch.object(heavy.os,'open') as opened,patch.object(heavy.subprocess,'run') as execute,contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(heavy.run(args),0)
            opened.assert_not_called()
            execute.assert_not_called()

    def test_17_orca_and_unknown_ancestry_protected(self):
        info={10:dict(process(10,1,'Orca'),path='/Applications/Orca.app/Contents/MacOS/Orca'),11:process(11,10),12:process(12,11)}
        self.assertTrue(ownership.orca_managed(12,info))
        self.assertIsNone(ownership.orca_managed(999,info))
        info[11]['ppid']=1
        self.assertFalse(ownership.orca_managed(11,info))
        info[11]['ppid']=99
        g=cleanup.classify_group(100,[11,12],info,cwd_query=lambda p:str(self.root),ports_query=lambda:{},tty_query=lambda:set(),app_query=lambda p:set(),parent_query=lambda p:True)
        self.assertTrue(g['orca_unknown'])
        self.assertTrue(g['protected'])

    def test_18_disk_inventory_deleted_worktree_and_safe_delete(self):
        repo=self.root/'repo'
        target=repo/'node_modules'
        target.mkdir(parents=True)
        (repo/'package.json').write_text('{}')
        (target/'data').write_bytes(b'x'*8192)
        meta=repo/'.git/worktrees/old'
        meta.mkdir(parents=True)
        (meta/'gitdir').write_text(str(self.root/'removed/.git'))
        history.append('sessions',{'cwd':str(self.root/'removed')})
        rows=disk.collect()['items']
        self.assertTrue(any(r['kind']=='node_modules' and r['bytes']>0 for r in rows))
        self.assertTrue(any(r['kind']=='git-worktree-metadata' and r['abandoned'] for r in rows))
        self.assertTrue(any(r['kind']=='deleted-worktree' for r in rows))
        self.assertFalse(disk.clean(str(target)))
        with patch.object(cleanup,'analyze',return_value=({},[])):
            self.assertTrue(disk.clean(str(target),yes=True))
        self.assertFalse(target.exists())
        self.assertFalse(disk.clean(str(repo),yes=True))

    def test_18_disk_refuses_active_workspace_and_symlinks(self):
        target=self.root/'repo/build'
        target.mkdir(parents=True)
        info={100:process(100,1)}
        with patch.object(cleanup,'analyze',return_value=(info,[])),patch.object(cleanup,'proc_cwd',return_value=str(target.parent)):
            self.assertFalse(disk.clean(str(target),yes=True))
        linked=self.root/'node_modules'
        linked.symlink_to(target,target_is_directory=True)
        self.assertFalse(disk.clean(str(linked),yes=True))
        self.assertTrue(target.exists())

    def test_19_doctor_offline_redacts_credentials_and_quota(self):
        home=self.root/'home'
        (home/'.codex/sessions').mkdir(parents=True)
        (home/'.claude').mkdir()
        (home/'.local/bin').mkdir(parents=True)
        (home/'.codex/auth.json').write_text(json.dumps({'tokens':{'access_token':'DO-NOT-OUTPUT'}}))
        (home/'.claude/.credentials.json').write_text(json.dumps({'claudeAiOauth':{'accessToken':'DO-NOT-OUTPUT','expiresAt':1}}))
        (home/'.claude/settings.json').write_text(json.dumps({'hooks':{'SessionEnd':[{'hooks':[{'command':'agentscope-hook session-end'}]}]}}))
        (home/'.codex/config.toml').write_text('notify = ["codex-notify-wrapper.sh"]\n')
        (home/'.local/bin/codex-notify-wrapper.sh').write_text('agentscope-hook')
        (home/'.codex/sessions/rollout-test.jsonl').write_text(json.dumps({'payload':{'rate_limits':{'primary':{'used_percent':30,'token':'DO-NOT-OUTPUT'}}}})+'\n')
        with patch.object(subprocess,'run',side_effect=AssertionError('doctor must not execute anything')):
            result=doctor.collect(home)
        self.assertTrue(result['offline'])
        self.assertNotIn('DO-NOT-OUTPUT',json.dumps(result))
        self.assertEqual(result['quota'][0]['limits']['primary'],{'used_percent':30})
        self.assertEqual(result['hooks']['status'],'configured')
        self.assertTrue(result['credentials'][1]['expired_hint'])
        (home/'.codex/config.toml').write_text('notify = ["other"]\n')
        self.assertEqual(doctor.collect(home)['notify']['status'],'missing-or-overwritten')

    def test_20_metrics_rotation_and_timeline(self):
        for index in range(6):
            self.assertTrue(metrics.write({'load':[index,0,0],'sessions':[]},max_bytes=1))
        files=list((self.root/'state').glob('metrics*.jsonl'))
        self.assertEqual(len(files),4)
        rows=metrics.timeline('1h')
        self.assertEqual([r['load'][0] for r in rows],[2,3,4,5])
        self.assertEqual(metrics.timeline('2999-01-01T00:00:00Z'),[])
        with self.assertRaises(ValueError):
            metrics.since('invalid')

    def test_20_metrics_lightweight_resources(self):
        info={100:dict(process(100,1,'codex'),path='/bin/codex',cwd='/work')}
        result=types.SimpleNamespace(returncode=0,stdout='Mach Virtual Memory Statistics: (page size of 16384 bytes)\nPages free: 2.\nPages active: 3.\n')
        with patch.object(metrics.procstat,'task_resources',return_value={'rss_bytes':123,'cpu_ticks':456}),patch.object(metrics.subprocess,'run',return_value=result),patch.object(metrics.os,'getloadavg',return_value=(1,2,3)):
            sample=metrics.sample(info,[])
        self.assertEqual(sample['sessions'][0]['rss_bytes'],123)
        self.assertEqual(sample['memory']['free_bytes'],32768)

    def test_21_same_cwd_conflict_main_sessions_only(self):
        rows=[{'pid':1,'cwd':str(self.root),'is_main':True},
              {'pid':2,'cwd':str(self.root),'is_main':True},
              {'pid':3,'cwd':str(self.root),'is_main':False}]
        self.assertEqual(activity.conflicts(rows)[0]['pids'],[1,2])
        alerts=agents.alerts(rows)
        self.assertEqual(alerts[0]['kind'],'CWD_CONFLICT')
