"""Validated user configuration; invalid safety settings fail closed."""
import fnmatch
import json
import math
import os

DEFAULT_PROTECT = ['WorkBuddy', 'Logseq', 'ChatGPT', 'Claude.app', 'LarkSuite',
                   'Orca', 'Paseo', 'Raycast', 'OrbStack', 'Terminal', 'iTerm']


def load_config(path=None):
    path = path or os.environ.get('AGENTSCOPE_CONFIG', '~/.config/agentscope/config.json')
    cfg = {'workspaces': [os.path.expanduser('~/' + p) for p in
           ('Workspace', 'Worksapce', 'Workbuddy', 'orca/workspaces', '.codex/worktrees',
            '.claude', 'Desktop', 'Documents', 'Projects')],
           'protect': {'comm': list(DEFAULT_PROTECT), 'cmdline': list(DEFAULT_PROTECT), 'cwd': []},
           'notify': {'enabled': False, 'cooldown_minutes': 15}, 'pricing': {}}
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
    for k in ('protect', 'notify', 'pricing'):
        if k in data:
            if not isinstance(data[k], dict):
                raise ValueError(k + ' must be an object')
            cfg[k].update(data[k])
    for k, values in cfg['protect'].items():
        if k not in ('comm', 'cmdline', 'cwd') or not isinstance(values, list) or not all(isinstance(v, str) and v for v in values):
            raise ValueError('protect fields must be string lists: comm/cmdline/cwd')
    n = cfg['notify']
    if not isinstance(n['enabled'], bool) or isinstance(n['cooldown_minutes'], bool) or not isinstance(n['cooldown_minutes'], (int, float)) or not math.isfinite(n['cooldown_minutes']) or n['cooldown_minutes'] < 0:
        raise ValueError('invalid notify configuration')
    for model, rates in cfg['pricing'].items():
        if not isinstance(rates, dict) or set(rates) != {'input', 'output', 'cacheWrite', 'cacheRead'} or any(isinstance(v, bool) or not isinstance(v, (int, float)) or not math.isfinite(v) or v < 0 for v in rates.values()):
            raise ValueError('pricing requires four finite nonnegative rates per model')
    env = os.environ.get('AGENTSCOPE_WORKSPACES', '').strip()
    if env:
        cfg['workspaces'] = [p for p in env.split(os.pathsep) if p]
    cfg['workspaces'] = [os.path.realpath(os.path.expanduser(p)) for p in cfg['workspaces']]
    return cfg


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
