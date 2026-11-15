"""
进程与资源采集。

为什么不用 ps / top：
  受限环境（沙箱 / seatbelt）下直接被拒绝：
      PermissionError: [Errno 1] Operation not permitted: 'ps'
  sysctl（KERN_PROC / KERN_PROCARGS2）同样不可用。

因此直接读内核进程表：
  - libproc.proc_listallpids()  枚举全部 PID
  - libproc.proc_pidinfo()      读取进程信息（flavor 3 / 4）
  - libc.getpgid()              读取进程组
  - /usr/bin/footprint          精确物理内存占用（JSON 模式）

已验证的字段偏移：
  flavor 3 (PROC_PIDTBSDINFO)      pid@12 ppid@16 comm@48 name@64
  flavor 4 (PROC_PIDT_SHORTBSDINFO) user_time@16 system_time@24  （单位：微秒）
"""

import ctypes
import ctypes.util
import json
import os
import subprocess
import tempfile
import time

_libc = ctypes.CDLL(ctypes.util.find_library("c"))
_libproc = ctypes.CDLL("/usr/lib/libSystem.B.dylib")

_libproc.proc_listallpids.argtypes = [ctypes.c_void_p, ctypes.c_int]
_libproc.proc_listallpids.restype = ctypes.c_int
_libproc.proc_pidinfo.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64,
                                  ctypes.c_void_p, ctypes.c_int]
_libproc.proc_pidinfo.restype = ctypes.c_int
_libproc.proc_pidpath.argtypes = [ctypes.c_int, ctypes.c_void_p, ctypes.c_uint32]
_libproc.proc_pidpath.restype = ctypes.c_int

_libc.getpgid.argtypes = [ctypes.c_int]
_libc.getpgid.restype = ctypes.c_int

PROC_PIDTBSDINFO = 3
PROC_PIDT_SHORTBSDINFO = 4


def _i32(b, o):
    return int.from_bytes(b[o:o + 4], "little", signed=True)


def _i64(b, o):
    return int.from_bytes(b[o:o + 8], "little", signed=True)


def list_pids():
    n = _libproc.proc_listallpids(None, 0)
    if n <= 0:
        return []
    buf = ctypes.create_string_buffer(n * 4)
    c = _libproc.proc_listallpids(buf, n * 4)
    return [int.from_bytes(buf.raw[i * 4:i * 4 + 4], "little") for i in range(c)]


def proc_bsdinfo(pid):
    """基础进程信息：pid / ppid / comm / name"""
    raw = ctypes.create_string_buffer(1024)
    r = _libproc.proc_pidinfo(pid, PROC_PIDTBSDINFO, 0, raw, 1024)
    if r <= 0:
        return None
    d = raw.raw[:r]
    if len(d) < 96:
        return None
    return {
        "start_time": _i64(d, 120) * 1000000 + _i64(d, 128) if len(d) >= 136 else 0,
        "pid": _i32(d, 12),
        "ppid": _i32(d, 16),
        "uid": _i32(d, 20),
        "status": _i32(d, 4),
        "comm": d[48:64].split(b"\0")[0].decode(errors="replace"),
        "name": d[64:96].split(b"\0")[0].decode(errors="replace"),
    }


def proc_cpu_us(pid):
    """进程累计 CPU 时间（微秒）。两次采样做差即得该期间的 CPU 占用。"""
    raw = ctypes.create_string_buffer(1024)
    r = _libproc.proc_pidinfo(pid, PROC_PIDT_SHORTBSDINFO, 0, raw, 1024)
    if r <= 0:
        return None
    d = raw.raw[:r]
    if len(d) < 32:
        return None
    user = _i64(d, 16)
    system = _i64(d, 24)
    if user < 0 or system < 0:
        return None
    return user + system


def proc_pgid(pid):
    g = _libc.getpgid(pid)
    return g if g >= 0 else -1


def proc_path(pid):
    """进程的可执行文件完整路径"""
    buf = ctypes.create_string_buffer(4096)
    r = _libproc.proc_pidpath(pid, buf, 4096)
    if r <= 0:
        return ""
    return bytes(buf.raw).split(b"\0")[0].decode(errors="replace")


def proc_parent(pid):
    """父进程 PID"""
    b = proc_bsdinfo(pid)
    return b["ppid"] if b else 0


def scan_processes():
    """全量进程快照"""
    out = {}
    for pid in list_pids():
        b = proc_bsdinfo(pid)
        if b and b["pid"] == pid:
            b["pgid"] = proc_pgid(pid)
            b["path"] = proc_path(pid)
            out[pid] = b
    try:
        result = subprocess.run(['ps', '-axo', 'pid=,command='], capture_output=True, text=True, timeout=10)
        for line in result.stdout.splitlines():
            parts = line.strip().split(None, 1)
            if len(parts) == 2 and parts[0].isdigit() and int(parts[0]) in out:
                out[int(parts[0])]['cmdline'] = parts[1]
    except (OSError, subprocess.TimeoutExpired):
        pass
    return out


# ── cwd / 端口 / 内存：批量采集 ─────────────────────────────────

def load_cwds(pids):
    """批量取 cwd。lsof 的 -F pn 输出格式为 p<pid> 后跟 n<path>。"""
    result = {}
    if not pids:
        return result
    try:
        r = subprocess.run(
            ["lsof", "-a", "-d", "cwd", "-F", "pn", "-p",
             ",".join(str(p) for p in pids)],
            capture_output=True, text=True, timeout=30).stdout
        cur = None
        for line in r.splitlines():
            if line.startswith("p"):
                cur = int(line[1:]) if line[1:].isdigit() else None
            elif line.startswith("n") and cur is not None:
                result[cur] = line[1:]
                cur = None
    except Exception:
        pass
    return result


def load_listening_ports():
    """pid -> [监听地址列表]"""
    result = {}
    try:
        r = subprocess.run(["lsof", "-nP", "-iTCP", "-sTCP:LISTEN"],
                           capture_output=True, text=True, timeout=25).stdout
        for line in r.splitlines()[1:]:
            parts = line.split()
            if len(parts) >= 9 and parts[1].isdigit():
                addr = parts[-1]
                if addr.startswith(("*:", "127.0.0.1:", "localhost:", "[::1]:")):
                    result.setdefault(int(parts[1]), []).append(addr)
    except Exception:
        pass
    return result


def footprint_mb(pid):
    """单个进程的物理内存占用（MB）。

    footprint 一次只接受一个 pid，所以逐个调用。实测每个约 15ms。
    """
    try:
        r = subprocess.run(["footprint", "-p", str(pid)],
                           capture_output=True, text=True, timeout=20).stdout
        for line in r.splitlines():
            if "Footprint:" in line:
                # "xxx [123]: 64-bit    Footprint: 18 MB (16384 bytes per page)"
                seg = line.split("Footprint:")[1].strip()
                num = seg.split()[0]
                unit = seg.split()[1] if len(seg.split()) > 1 else "B"
                val = float(num)
                if unit == "KB":
                    val /= 1024
                elif unit == "GB":
                    val *= 1024
                elif unit == "B":
                    val /= 1024 * 1024
                return val
    except Exception:
        pass
    return None


def footprint_many(pids):
    """批量取内存占用 —— 返回 {pid: MB}

    footprint 的 -p 参数只接受单个 pid，但可以连续传多个（取第一个）。
    因此这里按需逐个调用，并对失败项静默跳过。
    """
    out = {}
    for pid in pids:
        v = footprint_mb(pid)
        if v is not None:
            out[pid] = v
    return out


def cpu_percent(snapshots, interval=0.35):
    """由两次 CPU 时间快照计算占用百分比。

    snapshots: {pid: cpu_us}
    返回 {pid: percent}
    """
    time.sleep(interval)
    result = {}
    for pid, t0 in snapshots.items():
        t1 = proc_cpu_us(pid)
        if t1 is None or t0 is None:
            continue
        delta = t1 - t0
        if delta < 0:
            continue
        result[pid] = min(100.0, (delta / 1e6) / interval * 100)
    return result


def cpu_snapshot(pids):
    """采一批进程的 CPU 时间快照"""
    out = {}
    for pid in pids:
        v = proc_cpu_us(pid)
        if v is not None:
            out[pid] = v
    return out


def read_file_tail(path, max_lines=400, max_bytes=2 * 1024 * 1024, from_head=False):
    """读文件末尾若干行 —— 会话文件可能很大，只取尾部。

    from_head=True 时改为读文件开头若干行（用于取 session_meta 之类的元数据）。
    """
    try:
        size = os.path.getsize(path)
        with open(path, "rb") as f:
            if from_head:
                data = f.read(max_bytes)
            else:
                if size > max_bytes:
                    f.seek(size - max_bytes)
                    f.readline()   # 丢弃可能被截断的首行
                data = f.read()
        text = data.decode("utf-8", errors="replace")
        lines = text.split("\n")
        if len(lines) > max_lines:
            lines = lines[:max_lines] if from_head else lines[-max_lines:]
        return lines
    except Exception:
        return []
