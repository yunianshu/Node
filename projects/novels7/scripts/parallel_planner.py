#!/usr/bin/env python3
"""
并行Planner启动器 - 同时启动多个Planner处理不同章节范围
"""
import json
import subprocess
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from novels.core.config import NovelConfig


def merge_outlines(config: NovelConfig, parts_dir: Path):
    """合并所有part文件到outline.json"""
    all_chapters = []
    for part_file in sorted(parts_dir.glob("outline_part_*.json")):
        try:
            with open(part_file, "r", encoding="utf-8") as f:
                data = json.load(f)
            chapters = data.get("chapters", [])
            all_chapters.extend(chapters)
            print(f"  合并 {part_file.name}: {len(chapters)} 章")
        except Exception as e:
            print(f"  跳过 {part_file.name}: {e}")

    # 去重并按章节号排序
    seen = set()
    unique = []
    for ch in all_chapters:
        num = ch.get("chapter_number", 0)
        if num and num not in seen:
            seen.add(num)
            unique.append(ch)

    unique.sort(key=lambda x: x.get("chapter_number", 0))

    with open(config.outline_file, "w", encoding="utf-8") as f:
        json.dump({"chapters": unique}, f, ensure_ascii=False, indent=2)

    print(f"\n✅ 合并完成: outline.json 共 {len(unique)} 章")
    missing = [i for i in range(1, 2001) if i not in seen]
    if missing:
        print(f"⚠️ 缺失: {len(missing)} 章 - {missing[:20]}...")
    else:
        print("✅ 无缺失章节！")


def main():
    config_path = Path(__file__).parent.parent / "config.json"
    config = NovelConfig.load(config_path)

    # 创建parts目录
    parts_dir = config.output_dir / "outline_parts"
    parts_dir.mkdir(parents=True, exist_ok=True)

    # 将现有outline.json复制为part文件（如果有）
    if config.outline_file.exists():
        import shutil
        shutil.copy(config.outline_file, parts_dir / "outline_part_existing.json")
        print(f"已复制现有 outline.json 到 {parts_dir / 'outline_part_existing.json'}")

    # 定义4个并行段
    segments = [
        (1, 500, "outline_part_1_500.json"),
        (501, 1000, "outline_part_501_1000.json"),
        (1001, 1500, "outline_part_1001_1500.json"),
        (1501, 2000, "outline_part_1501_2000.json"),
    ]

    planner_script = Path(__file__).parent / "planner.py"
    processes = []

    print("=" * 60)
    print("启动4个并行Planner")
    print("=" * 60)

    for start, end, output_name in segments:
        output_path = parts_dir / output_name
        cmd = [
            sys.executable, str(planner_script),
            "--start", str(start),
            "--end", str(end),
            "--batch-size", "50",
            "--output", str(output_path),
        ]
        print(f"\n启动: 第{start}-{end}章 -> {output_name}")
        p = subprocess.Popen(cmd)
        processes.append((p, start, end, output_name))
        time.sleep(2)  # 错开启动时间

    # 等待所有进程完成
    print("\n" + "=" * 60)
    print("等待所有Planner完成...")
    print("=" * 60)

    while True:
        all_done = True
        for p, start, end, name in processes:
            if p.poll() is None:
                all_done = False
                print(f"  第{start}-{end}章 运行中...")
        if all_done:
            break
        time.sleep(30)

    print("\n所有Planner已完成，开始合并...")
    merge_outlines(config, parts_dir)

    # 启动Coordinator进入Writer阶段
    print("\n启动Coordinator进入Writer阶段...")
    coordinator_script = config.path.parent.parent / "novels" / "coordinator.py"
    if coordinator_script.exists():
        subprocess.Popen([
            sys.executable, str(coordinator_script),
            "--config", str(config_path),
            "--skip-planner",
        ])
        print("Coordinator已启动")
    else:
        print(f"未找到coordinator: {coordinator_script}")


if __name__ == "__main__":
    main()
