"""Single daemon worker: slow sampling never holds terminal input or races cleanup caches."""
import queue
import threading
import time
from . import agents, cleanup, nodeheap
from .config import load_config
from .notify import Notifier


class PanelWorker:
    def __init__(self, args):
        self.args = args
        self.results = queue.Queue()
        self.jobs = queue.Queue()
        self.stop = threading.Event()
        self.thread = threading.Thread(target=self._run, daemon=True)
        self.thread.start()

    def request(self, group):
        self.jobs.put(group)

    def poll(self):
        try:
            return self.results.get_nowait()
        except queue.Empty:
            return None

    def close(self):
        self.stop.set()

    def _run(self):
        notifier = Notifier()
        group_due = 0
        info, groups = {}, []
        while not self.stop.is_set():
            try:
                try:
                    group = self.jobs.get_nowait()
                except queue.Empty:
                    group = None
                if group is not None:
                    outcome = {}
                    cleanup.kill_group(group['pgid'], mode='panel', expected=group, outcome=outcome)
                    self.results.put({'message': outcome.get('result', 'skipped')})
                    group_due = 0
                if self.stop.is_set():
                    return
                cfg = load_config()
                if time.monotonic() >= group_due:
                    info, groups = cleanup.analyze(cfg=cfg)
                    group_due = time.monotonic() + 20
                    for g in groups:
                        g['can_clean'] = cleanup.cleanable(g, info, cfg=cfg)
                sess = agents.collect(context_limit=self.args.context_limit)
                nodes = nodeheap.collect() if self.args.node else None
                notifier.send(agents.alerts(sess), cfg=cfg)
                self.results.put({'info': info, 'groups': groups, 'sess': sess, 'nodes': nodes})
            except Exception as error:
                self.results.put({'message': f'刷新失败: {error}'})
            self.stop.wait(max(.1, self.args.refresh))
