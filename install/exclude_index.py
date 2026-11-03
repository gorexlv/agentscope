#!/usr/bin/env python3
"""
批量写入 .metadata_never_index，把可再生成的目录从 Spotlight 索引中排除。

为什么需要：
  Agent 长期堆积的另一半原因，是 Spotlight 在反复扫描 node_modules。
  117 GB 的工作区里有 653 个 node_modules、约 58 GB，对检索毫无价值，
  却让索引进程持续吃满磁盘 IO。

用法：
  python3 install/exclude_index.py --dry-run    # 预演
  python3 install/exclude_index.py              # 执行
  python3 install/exclude_index.py --include-dist  # 连 dist/build 一起排除
"""

import argparse
import json
import os
import subprocess
import sys
import time

HOME = os.path.expanduser("~")

# 默认扫描的根目录
DEFAULT_ROOTS = [
    os.path.join(HOME, "Workspace"),
    os.path.join(HOME, "Worksapce"),      # 本机存在此拼写错误目录
    os.path.join(HOME, "orca", "workspaces"),
    os.path.join(HOME, "Projects"),
    os.path.join(HOME, "code"),
]

# 永远排除 —— 这些都是可再生成的产物，对 Spotlight 检索无价值
SAFE_NAMES = {
    "node_modules", "__pycache__", ".cache", "coverage",
    ".turbo", ".pytest_cache", ".mypy_cache", ".ruff_cache",
    ".parcel-cache", ".gradle", ".cargo", ".venv", "venv",
    "target",                          # Rust 构建目录
    ".pnpm-store", ".yarn", ".npm",
}

# 默认不排除 —— 可能是人工维护的产物
OPTIONAL_NAMES = {"dist", "build", ".next", "out"}

MARKER = ".metadata_never_index"


def run(cmd, timeout=240):
    try:
        return subprocess.run(cmd, capture_output=True, text=True,
                              timeout=timeout).stdout
    except Exception:
        return ""


def find_targets(roots, names, maxdepth=5):
    """一次性 find 出所有目标目录（排除嵌套的 node_modules 内部）"""
    existing = [r for r in roots if os.path.isdir(r)]
    if not existing:
        return []

    expr = []
    for n in sorted(names):
        if expr:
            expr.append("-o")
        expr += ["-name", n]
    # 排除 node_modules 内部的嵌套 node_modules
    # 注意：括号内必须以 "-name xxx" 开头，不能写 "-o -name xxx"
    #       否则 find 把它当成「-o 匹配任意」而选中所有目录
    cmd = ["find"] + existing + [
        "-maxdepth", str(maxdepth), "-type", "d",
        "("] + expr + [")",
        "-not", "-path", "*/node_modules/*/node_modules/*",
    ]
    return sorted({x for x in run(cmd).splitlines() if x.strip()})


def measure_sizes(paths, batch=100):
    """批量统计体积 —— du -sk 比逐目录 find 快一个数量级。

    必须分批：一次传几百个路径会超出命令行长度限制（ARG_MAX），
    导致 du 静默失败、返回空结果。
    """
    if not paths:
        return []
    sizes = []
    for i in range(0, len(paths), batch):
        chunk = paths[i:i + batch]
        out = run(["du", "-sk"] + chunk, timeout=300)
        for line in out.splitlines():
            parts = line.split("\t")
            if len(parts) == 2 and parts[0].isdigit():
                sizes.append((int(parts[0]), parts[1]))
    sizes.sort(reverse=True)
    return sizes


def group_by_name(sizes):
    cats = {}
    for kb, t in sizes:
        base = os.path.basename(t)
        cats.setdefault(base, [0, 0])
        cats[base][0] += kb
        cats[base][1] += 1
    return cats


def main():
    ap = argparse.ArgumentParser(description="批量排除 Spotlight 索引")
    ap.add_argument("--dry-run", action="store_true", help="只预演不写入")
    ap.add_argument("--include-dist", action="store_true",
                    help="连 dist/build/.next 一起排除（可能是人工维护产物）")
    ap.add_argument("--root", action="append", default=[],
                    help="额外指定扫描根目录，可重复")
    ap.add_argument("--maxdepth", type=int, default=5, help="扫描深度")
    ap.add_argument("--json", help="把结果写到指定 JSON 文件")
    args = ap.parse_args()

    names = set(SAFE_NAMES)
    if args.include_dist:
        names |= OPTIONAL_NAMES

    roots = DEFAULT_ROOTS + args.root
    print(f"\n扫描根目录:")
    for r in roots:
        mark = "✓" if os.path.isdir(r) else "·"
        print(f"  {mark} {r}")
    print()

    targets = find_targets(roots, names, args.maxdepth)
    if not targets:
        print("未发现需要排除的目录。")
        return 0

    print(f"发现 {len(targets)} 个目标目录，统计体积中...")
    sizes = measure_sizes(targets)
    total_kb = sum(k for k, _ in sizes)

    print(f"\n{'=' * 66}")
    print(f"合计 {len(sizes)} 个目录 / {total_kb / 1024 / 1024:.1f} GB")
    print("=" * 66)

    print("\n=== 体积 Top 12 ===")
    for kb, t in sizes[:12]:
        print(f"  {kb / 1024:>9.1f} MB  {t.replace(HOME, '~')}")

    cats = group_by_name(sizes)
    print("\n=== 按类别汇总 ===")
    for k, (kb, n) in sorted(cats.items(), key=lambda x: -x[1][0])[:12]:
        print(f"  {k:<16} {kb / 1024 / 1024:>8.1f} GB  {n:>5} 个目录")

    if args.dry_run:
        print("\n[dry-run] 未写入任何文件。加 --dry-run 去掉即可实际执行。")
        if args.json:
            with open(args.json, "w", encoding="utf-8") as f:
                json.dump([t for _, t in sizes], f)
            print(f"清单已写入 {args.json}")
        return 0

    print("\n写入 .metadata_never_index ...")
    created = 0
    failed = []
    for t in sizes:
        _, d = t
        marker = os.path.join(d, MARKER)
        if os.path.exists(marker):
            continue
        try:
            # O_CREAT|O_EXCL —— 不覆盖已有文件
            fd = os.open(marker, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
            os.write(fd, b"Exclude this directory from Spotlight indexing.\n")
            os.close(fd)
            created += 1
        except FileExistsError:
            pass
        except Exception as e:
            failed.append((d, str(e)))

    print(f"✓ 新建 {created} 个标记文件")
    if failed:
        print(f"✗ 失败 {len(failed)} 个:")
        for d, e in failed[:8]:
            print(f"    {d.replace(HOME, '~')}: {e}")

    # 验证
    sample = [d for _, d in sizes[:3]]
    print("\n=== 验证样本 ===")
    for d in sample:
        ok = os.path.exists(os.path.join(d, MARKER))
        print(f"  {'✓' if ok else '✗'} {d.replace(HOME, '~')}")

    print(f"\n完成：新排除 {created} 个目录。"
          f"新克隆的仓库需要重跑本脚本才能补充排除。")

    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump([d for _, d in sizes], f)
        print(f"清单已写入 {args.json}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
