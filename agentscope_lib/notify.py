"""Rate-limited macOS notifications, text passed as argv rather than AppleScript."""
import subprocess
import time
from .config import load_config


class Notifier:
    def __init__(self):
        self.sent = {}

    def send(self, alerts):
        cfg = load_config()['notify']
        if not cfg['enabled']:
            return
        now = time.monotonic()
        cooldown = cfg['cooldown_minutes'] * 60
        self.sent = {k: t for k, t in self.sent.items() if now - t < cooldown}
        for alert in alerts:
            if alert['kind'] not in ('CONTEXT_LOW', 'MEM_HIGH', 'WATCH_CLEANED'):
                continue
            key = (alert.get('pid'), alert['kind'])
            if key in self.sent:
                continue
            try:
                result = subprocess.run(['osascript', '-e', 'on run argv\ndisplay notification (item 1 of argv) with title "agentscope"\nend run', alert['msg']], capture_output=True, timeout=5)
                if result.returncode == 0:
                    self.sent[key] = now
            except (OSError, subprocess.TimeoutExpired):
                pass


def group_key(group):
    return (group['pgid'], tuple((p, t) for p, t in group['root_starts']))


def expire_seen(seen, groups):
    seen.intersection_update(group_key(g) for g in groups)
