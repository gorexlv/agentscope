"""
持续监控循环。

供 agents / scan / node / stats 等子命令的 --watch 共用。

与 panel 的区别：
  panel   交互式 TUI，全屏重绘，带 node 内存段与告警，适合盯着看
  --watch 轻量版，按间隔刷新同一块内容，可用 --no-clear 关掉清屏
          （方便 pipe 到 grep / less）

行为约定：
  - 默认清屏重绘（像 watch 命令）
  - --no-clear 时逐块追加输出，适合管道
  - Ctrl-C 优雅退出，光标与终端状态复原
  - --iterations N 限定刷新轮数（便于脚本与测试）
"""

import sys
import time


def run_watch(collect_fn, render_fn, interval=3, clear=True,
              iterations=0, header=None):
    """持续监控主循环。

    collect_fn: 每轮调用，返回数据
    render_fn: 接收数据，返回要打印的文本
    interval:  刷新间隔（秒）
    clear:     是否清屏重绘
    iterations: 刷新轮数，0 表示无限
    header:    可选的固定头部（如说明文字）
    """
    import os

    try:
        sys.stdout.write("\x1b[?25l")   # 隐藏光标
        n = 0
        while True:
            n += 1
            if clear and n > 1:
                sys.stdout.write("\x1b[2J\x1b[H")
            elif n > 1 and not clear:
                sys.stdout.write("\n")

            if header:
                sys.stdout.write(header + "\n")

            try:
                data = collect_fn()
                text = render_fn(data)
                if text:
                    sys.stdout.write(text if text.endswith("\n") else text + "\n")
                sys.stdout.flush()
            except KeyboardInterrupt:
                raise
            except Exception as e:
                sys.stdout.write(f"\x1b[31m采集出错: {e}\x1b[0m\n")
                sys.stdout.flush()

            if iterations and n >= iterations:
                break
            time.sleep(interval)

    except KeyboardInterrupt:
        sys.stdout.write("\n\x1b[2m已停止监控\x1b[0m\n")
    finally:
        sys.stdout.write("\x1b[?25h\x1b[0m\n")   # 恢复光标
        sys.stdout.flush()
    return 0
