"""Local Unix socket service used by the menubar client.

契约：runs/menubar-20261116/contract.md

协议：每个连接一行 JSON 请求、一行 JSON 响应，clean 超时 30s，其余 5s。
  {"op":"ping"}                            → {"ok":true,"version":"…"}
  {"op":"snapshot"}                        → {"ok":true,"snapshot":{…}}
  {"op":"clean","dry_run":true}            → {"ok":true,"token":"…","targets":[…]}
  {"op":"clean","dry_run":false,"token":…} → {"ok":true,"cleaned":[…],"gone":[…],"skipped":[…]}
  其它                                     → {"ok":false,"error":"…"}

安全约束：
  - socket 权限 0600，仅本用户
  - 清理必须先 dry_run 拿 token；token 一次性、60s 过期
  - 只清理 token 里列出的组，且执行前再做一次 cleanable 复核
  - 清理走既有 kill_group（内部会复核），审计 mode=socket
  - 服务异常绝不能影响守护巡检：所有异常都在线程内吞掉
"""

import copy
import ctypes
import struct
import sys
import subprocess
import fcntl
import stat
import tempfile
import datetime
import json
import os
import secrets
import socket
import threading
import time

from . import history

TIMEOUT = 5.0
CLEAN_TIMEOUT = 30.0
MAX_CONNECTIONS = 8
TOKEN_TTL = 60.0
MAX_REQUEST = 65536
_request_context = threading.local()


def peer_uid(connection):
    """Kernel credentials, never client-supplied identity; fail closed."""
    if sys.platform == 'darwin':
        uid, gid = ctypes.c_uint(), ctypes.c_uint()
        libc = ctypes.CDLL(None, use_errno=True)
        if libc.getpeereid(connection.fileno(), ctypes.byref(uid), ctypes.byref(gid)):
            raise OSError(ctypes.get_errno(), 'getpeereid failed')
        return uid.value
    if hasattr(socket, 'SO_PEERCRED'):
        return struct.unpack('3i', connection.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))[1]
    raise OSError('peer credentials unsupported')


def socket_path():
    return os.path.join(history.state_dir(), 'agentscope.sock')


def _version():
    from . import cleanup
    return cleanup.VERSION


# ─────────────────────────────────────────────────────────────
# 默认的清理实现（复用 cleanup 模块的判定与执行）
# ─────────────────────────────────────────────────────────────

def default_clean_targets():
    """当前可清理的孤儿组（复用 cleanable 判定）。"""
    from . import cleanup
    cfg = cleanup.load_config()
    info, units = cleanup.analyze(cfg=cfg, deadline=getattr(_request_context, 'deadline', None))
    return [group for group in units if cleanup.cleanable(group, info, cfg=cfg)]


def default_clean_group(group):
    """执行清理：kill_group 内部会再次复核，审计 mode=socket。"""
    from . import cleanup
    outcome = {}
    ok = cleanup.kill_group(group['pgid'], expected=group, mode='socket', outcome=outcome,
                            deadline=getattr(_request_context, 'deadline', None), timeout=2)
    return ok, outcome.get('result'), outcome.get('reason')


# ─────────────────────────────────────────────────────────────
# 服务
# ─────────────────────────────────────────────────────────────

class SnapshotServer:
    """后台线程里的 AF_UNIX 服务。"""

    def __init__(self, path=None, snapshot_provider=None, clean_targets=None,
                 clean_group=None, timeout=TIMEOUT, token_ttl=TOKEN_TTL,
                 daemon_info=None, refresh_interval=15.0, clean_cache=None,
                 allow_cleanup=True):
        self.path = path or socket_path()
        self.clean_cache = clean_cache
        self.allow_cleanup = allow_cleanup
        self._connections = threading.BoundedSemaphore(MAX_CONNECTIONS)
        self.timeout = timeout
        self.token_ttl = token_ttl
        self.refresh_interval = refresh_interval
        self.daemon_info = daemon_info if daemon_info is not None else {}
        self._injected_provider = snapshot_provider
        self.snapshot_provider = snapshot_provider or self._cached_snapshot
        self.clean_targets = clean_targets or default_clean_targets
        self.clean_group = clean_group or default_clean_group
        self._tokens = {}
        self._lock = threading.Lock()
        self._socket = None
        self._lease = None
        self._endpoint_identity = None
        self._thread = None
        self._refresher = None
        self._stop = threading.Event()
        self._snapshot_cache = None
        self._snapshot_at = 0.0

    # ── 生命周期 ────────────────────────────────────────────
    def start(self):
        directory = os.path.dirname(self.path) or '.'
        os.makedirs(directory, mode=0o700, exist_ok=True)
        directory_stat = os.stat(directory)
        if directory_stat.st_uid != os.getuid() or directory_stat.st_mode & 0o022:
            raise RuntimeError('state directory must be owned by current user and not writable by others')
        descriptor = os.open(self.path + '.lock', os.O_CREAT | os.O_WRONLY | os.O_NOFOLLOW, 0o600)
        lease = os.fdopen(descriptor, 'a')
        lease_stat = os.fstat(lease.fileno())
        if not stat.S_ISREG(lease_stat.st_mode) or lease_stat.st_uid != os.getuid():
            lease.close()
            raise RuntimeError('invalid socket lease')
        os.fchmod(lease.fileno(), 0o600)
        try:
            fcntl.flock(lease, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            lease.close()
            raise RuntimeError('socket server already running')
        server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            if os.path.lexists(self.path):
                existing = os.lstat(self.path)
                if not stat.S_ISSOCK(existing.st_mode) or existing.st_uid != os.getuid():
                    raise RuntimeError('refusing to replace non-owned socket path')
                with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as probe:
                    probe.settimeout(self.timeout)
                    try:
                        probe.connect(self.path)
                    except ConnectionRefusedError:
                        pass
                    else:
                        raise RuntimeError('socket server already running')
            # Publish only after permissions are private; never change process umask.
            with tempfile.TemporaryDirectory(prefix='.sock-', dir=directory) as staging:
                temporary = os.path.join(staging, 's')
                server.bind(temporary)
                os.chmod(temporary, 0o600)
                server.listen(16)
                os.replace(temporary, self.path)
            endpoint = os.stat(self.path)
            self._endpoint_identity = (endpoint.st_dev, endpoint.st_ino)
            server.settimeout(0.5)
        except Exception:
            server.close()
            lease.close()
            raise
        self._lease = lease
        self._socket = server
        self._stop.clear()
        self._thread = threading.Thread(target=self._serve, name='agentscope-socket', daemon=True)
        self._thread.start()
        if self._injected_provider is None:
            # 没有注入数据源时自己后台刷新，快照接口永远在 5s 内返回。
            self._refresher = threading.Thread(target=self._refresh_loop,
                                               name='agentscope-snapshot', daemon=True)
            self._refresher.start()
        return self

    def stop(self):
        self._stop.set()
        server, self._socket = self._socket, None
        if server is not None:
            try:
                server.close()
            except OSError:
                pass
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2)
        refresher, self._refresher = self._refresher, None
        if refresher is not None:
            refresher.join(timeout=2)
        try:
            endpoint = os.lstat(self.path)
            if (endpoint.st_dev, endpoint.st_ino) == self._endpoint_identity:
                os.unlink(self.path)
        except OSError:
            pass
        if self._lease is not None:
            self._lease.close()
            self._lease = None

    # ── 内部 ────────────────────────────────────────────────
    def _default_snapshot(self):
        from . import snapshot as snapshot_mod
        return snapshot_mod.build_snapshot(daemon=self.daemon_info or None)

    def _refresh_snapshot(self):
        try:
            self._snapshot_cache = self._default_snapshot()
            self._snapshot_at = time.monotonic()
        except Exception:
            pass

    def _refresh_loop(self):
        while not self._stop.is_set():
            self._refresh_snapshot()
            self._stop.wait(max(1.0, self.refresh_interval))

    def _cached_snapshot(self):
        """返回最近一次后台刷新出来的快照。

        快照重建要扫机器（几秒到十几秒），而协议要求 5s 内响应，
        所以这里只读缓存，绝不同步重建；缓存还没就绪时返回空壳快照。
        """
        if self._snapshot_cache is None:
            from . import snapshot as snapshot_mod
            return snapshot_mod.build_snapshot(sessions=[], info={}, units=[], alerts=[],
                                               load={}, daemon=self.daemon_info or None)
        return self._snapshot_cache

    def _serve(self):
        while not self._stop.is_set():
            try:
                connection, _ = self._socket.accept()
            except socket.timeout:
                continue
            except OSError:
                if self._stop.is_set():
                    return
                continue
            except Exception:
                continue
            try:
                if peer_uid(connection) != os.getuid():
                    connection.close()
                    continue
            except OSError:
                connection.close()
                continue
            if not self._connections.acquire(blocking=False):
                connection.settimeout(0.05)
                self._respond(connection, {'ok': False, 'error': 'busy, retry'})
                connection.close()
                continue
            try:
                worker = threading.Thread(target=self._safe_handle, args=(connection,),
                                          name='agentscope-socket-conn', daemon=True)
                worker.start()
            except Exception:
                connection.close()
                self._connections.release()
                continue

    def _safe_handle(self, connection):
        try:
            self._handle(connection)
        except Exception:
            pass
        finally:
            connection.close()
            self._connections.release()

    def _handle(self, connection):
        try:
            deadline = time.monotonic() + self.timeout
            connection.settimeout(self.timeout)
            buffer = b''
            while b'\n' not in buffer and len(buffer) < MAX_REQUEST:
                try:
                    connection.settimeout(max(0.001, deadline - time.monotonic()))
                    chunk = connection.recv(min(4096, MAX_REQUEST - len(buffer)))
                except socket.timeout:
                    self._respond(connection, {'ok': False, 'error': 'timeout'})
                    return
                if not chunk:
                    break
                buffer += chunk
            if b'\n' not in buffer:
                self._respond(connection, {'ok': False, 'error': 'unterminated or oversized request'})
                return
            line = buffer.split(b'\n', 1)[0].strip()
            if not line:
                self._respond(connection, {'ok': False, 'error': 'empty request'})
                return
            try:
                request = json.loads(line.decode('utf-8'))
            except (ValueError, UnicodeDecodeError):
                self._respond(connection, {'ok': False, 'error': 'invalid json'})
                return
            if not isinstance(request, dict):
                self._respond(connection, {'ok': False, 'error': 'invalid request'})
                return
            if request.get('op') == 'clean':
                deadline = time.monotonic() + CLEAN_TIMEOUT
                connection.settimeout(CLEAN_TIMEOUT)
            _request_context.deadline = deadline - 0.2
            self._respond(connection, self.dispatch(request))
        finally:
            try:
                connection.close()
            except OSError:
                pass

    def _respond(self, connection, payload):
        data = (json.dumps(payload, ensure_ascii=False, default=str) + '\n').encode('utf-8')
        try:
            connection.sendall(data)
        except OSError:
            pass

    # ── 协议 ────────────────────────────────────────────────
    def dispatch(self, request):
        op = request.get('op')
        try:
            if op == 'ping':
                return {'ok': True, 'version': _version(),
                        'pid': os.getpid(),
                        'interval_s': self.daemon_info.get('interval_s')}
            if op == 'snapshot':
                return {'ok': True, 'snapshot': self.snapshot_provider()}
            if op == 'clean':
                cache = self._fresh_cache() if request.get('dry_run') is True else None
                if cache is not None:
                    return self._clean(request, cache)
                from .cleanup import CLEANUP_LOCK
                if not CLEANUP_LOCK.acquire(blocking=False):
                    return {'ok': False, 'error': 'busy, retry'}
                try:
                    return self._clean(request, cache)
                finally:
                    CLEANUP_LOCK.release()
        except (TimeoutError, subprocess.TimeoutExpired):
            return {'ok': False, 'error': 'busy, retry'}
        except Exception as error:
            return {'ok': False, 'error': str(error)}
        return {'ok': False, 'error': 'unknown op'}

    def _fresh_cache(self):
        cache = self.clean_cache() if self.clean_cache else None
        interval = max(0.1, self.daemon_info.get('interval_s') or self.refresh_interval)
        if cache is not None and 0 <= time.monotonic() - cache['monotonic'] <= 2 * interval:
            return cache
        return None

    def _clean(self, request, cache=None):
        if type(request.get('dry_run')) is not bool:
            return {'ok': False, 'error': 'dry_run must be a boolean'}
        if request['dry_run']:
            if cache is not None:
                groups, source, as_of = cache['groups'], 'cache', cache['as_of']
            else:
                groups, source = self.clean_targets(), 'live'
                as_of = datetime.datetime.now().astimezone().isoformat()
            token = self._issue_token(groups)
            targets = [{'pgid': group['pgid'], 'size': group.get('size', 0),
                        'cwd': group.get('cwd'), 'names': group.get('names', [])}
                       for group in groups]
            return {'ok': True, 'token': token, 'targets': targets, 'source': source,
                    'as_of': as_of, 'executable': self.allow_cleanup}
        if not self.allow_cleanup:
            return {'ok': False, 'error': 'daemon is in dry-run mode'}
        groups = self._consume_token(request.get('token'))
        if groups is None:
            return {'ok': False, 'error': 'invalid or expired token'}
        cleaned, gone, skipped = [], [], []
        for group in groups:
            ok, result, reason = self.clean_group(group)
            if ok and result in ('cleaned', 'gone'):
                (cleaned if result == 'cleaned' else gone).append(group['pgid'])
                if result == 'cleaned':
                    self.daemon_info['last_clean_at'] = datetime.datetime.now().astimezone().isoformat()
            else:
                skipped.append({'pgid': group['pgid'],
                                'reason': reason or result or 'not cleanable'})
        return {'ok': True, 'cleaned': cleaned, 'gone': gone, 'skipped': skipped}

    # ── token ───────────────────────────────────────────────
    def _issue_token(self, groups):
        token = secrets.token_urlsafe(24)
        with self._lock:
            self._prune()
            self._tokens[token] = {'groups': copy.deepcopy(groups),
                                   'expires': time.monotonic() + self.token_ttl}
        return token

    def _consume_token(self, token):
        """一次性：取出即删除；过期视为无效。"""
        if not token or not isinstance(token, str):
            return None
        with self._lock:
            self._prune()
            entry = self._tokens.pop(token, None)
        if not entry:
            return None
        if entry['expires'] < time.monotonic():
            return None
        return entry['groups']

    def _prune(self):
        now = time.monotonic()
        for key in [k for k, v in self._tokens.items() if v['expires'] < now]:
            self._tokens.pop(key, None)


# ─────────────────────────────────────────────────────────────
# 客户端helper（CLI 与 App 共用同一套语义）
# ─────────────────────────────────────────────────────────────

def request(payload, path=None, timeout=None):
    """发一行请求读一行响应；失败返回 None。"""
    path = path or socket_path()
    timeout = timeout if timeout is not None else (CLEAN_TIMEOUT if payload.get('op') == 'clean' else TIMEOUT)
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as client:
            client.settimeout(timeout)
            client.connect(path)
            client.sendall((json.dumps(payload, ensure_ascii=False) + '\n').encode('utf-8'))
            buffer = b''
            while b'\n' not in buffer:
                chunk = client.recv(4096)
                if not chunk:
                    break
                buffer += chunk
        line = buffer.split(b'\n', 1)[0]
        return json.loads(line.decode('utf-8'))
    except (OSError, ValueError):
        return None


def ping(path=None, timeout=1.0):
    return request({'op': 'ping'}, path=path, timeout=timeout)
