"""Validated user configuration; invalid safety settings fail closed."""
import glob
import fnmatch
import json
import math
import os
import copy
import sys

DEFAULT_PROTECT = ['WorkBuddy', 'Logseq', 'ChatGPT', 'Claude.app', 'LarkSuite',
                   'Orca', 'Paseo', 'Raycast', 'OrbStack', 'Terminal', 'iTerm']


def _parse_config(path, data=None):
    path = path or os.environ.get('AGENTSCOPE_CONFIG', '~/.config/agentscope/config.json')
    cfg = {'workspaces': [os.path.expanduser('~/' + p) for p in
           ('Workspace', 'Worksapce', 'Workbuddy', 'orca/workspaces', '.codex/worktrees',
            '.claude', 'Desktop', 'Documents', 'Projects')],
           'protect': {'comm': [name + '*' for name in DEFAULT_PROTECT], 'cmdline': ['*/' + name.removesuffix('.app') + '.app/*' for name in DEFAULT_PROTECT], 'cwd': []},
           'worktrees': {'idle_days': 30, 'stale_days': 60, 'protect': []},
           'notify': {'enabled': False, 'cooldown_minutes': 15}, 'pricing': {}}
    if data is None:
        try:
            with open(os.path.expanduser(path), encoding='utf-8') as f:
                data = json.load(f)
        except FileNotFoundError:
            data = {}
    if not isinstance(data, dict):
        raise ValueError('config must be an object')
    for k in ('workspaces',):
        if k in data:
            if not isinstance(data[k], list) or not all(isinstance(v, str) and v for v in data[k]):
                raise ValueError(k + ' must be a list of nonempty strings')
            cfg[k] = data[k]
    for k in ('protect', 'notify', 'pricing', 'worktrees'):
        if k in data:
            if not isinstance(data[k], dict):
                raise ValueError(k + ' must be an object')
            cfg[k].update(data[k])
    for k, values in cfg['protect'].items():
        if k not in ('comm', 'cmdline', 'cwd') or not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
            raise ValueError('protect fields must be string lists: comm/cmdline/cwd')
    w = cfg['worktrees']
    for key in ('idle_days', 'stale_days'):
        if isinstance(w[key], bool) or not isinstance(w[key], (int, float)) or not math.isfinite(w[key]) or w[key] < 0:
            raise ValueError('invalid worktrees threshold')
    if not isinstance(w['protect'], list) or not all(isinstance(p, str) and p for p in w['protect']):
        raise ValueError('worktrees.protect must be a path list')
    w['protect'] = [os.path.realpath(os.path.expanduser(p)) for p in w['protect']]
    n = cfg['notify']
    if not isinstance(n['enabled'], bool) or isinstance(n['cooldown_minutes'], bool) or not isinstance(n['cooldown_minutes'], (int, float)) or not math.isfinite(n['cooldown_minutes']) or n['cooldown_minutes'] < 0:
        raise ValueError('invalid notify configuration')
    for model, rates in cfg['pricing'].items():
        if not isinstance(rates, dict) or set(rates) != {'input', 'output', 'cacheWrite', 'cacheRead'} or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in rates.values()):
            raise ValueError('pricing requires four finite nonnegative rates per model')
    env = os.environ.get('AGENTSCOPE_WORKSPACES', '').strip()
    if env:
        cfg['workspaces'] = [p for p in env.split(os.pathsep) if p]
    if 'workspaces' not in data and not env:
        cfg['workspaces'].extend(glob.glob(os.path.expanduser('~/multica_workspaces*')))
    cfg['workspaces'] = [os.path.realpath(os.path.expanduser(p)) for p in cfg['workspaces']]
    return cfg


_cache_key = None
_cache_value = None


def load_config(path=None, strict=False):
    """Cache one parsed file revision; readers fall back, cleaners inspect valid."""
    global _cache_key, _cache_value
    path = os.path.expanduser(path or os.environ.get('AGENTSCOPE_CONFIG', '~/.config/agentscope/config.json'))
    try:
        st = os.stat(path)
        stamp = (st.st_ino, st.st_mtime_ns, st.st_size, st.st_ctime_ns)
    except OSError as error:
        stamp = (type(error).__name__, str(error))
    key = (path, stamp, os.environ.get('AGENTSCOPE_WORKSPACES'))
    if key != _cache_key:
        try:
            value = _parse_config(path)
            value['valid'] = True
        except (OSError, ValueError, TypeError) as error:
            value = _parse_config(path, data={})
            value.update(valid=False, error=str(error))
            print(f'agentscope: invalid config; display defaults, cleanup disabled: {error}', file=sys.stderr, flush=True)
        _cache_key, _cache_value = key, value
    if strict and not _cache_value['valid']:
        raise ValueError(_cache_value['error'])
    return copy.deepcopy(_cache_value)


def protected(record, cwd, cfg):
    if cfg['protect'].get('cmdline') and 'cmdline' not in record:
        return True  # Cannot establish that command-line protection does not match.
    fields = {'comm': record.get('comm', '') + ' ' + record.get('name', ''),
              'cmdline': record.get('cmdline', ''), 'cwd': cwd}
    return any((fnmatch.fnmatchcase(fields[k].lower(), pattern.lower()) if any(c in pattern for c in '*?[')
                else pattern.lower() in fields[k].lower())
               for k, patterns in cfg['protect'].items() for pattern in patterns)


def within(path, roots):
    if not path:
        return False
    path = os.path.realpath(os.path.expanduser(path))
    return any(path == root or path.startswith(root.rstrip(os.sep) + os.sep) for root in roots)
