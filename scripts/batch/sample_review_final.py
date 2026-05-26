#!/usr/bin/env python3
"""抽样 review 终稿目录，快速评估终稿质量"""
import json, random, sys, subprocess, os
from pathlib import Path

project = sys.argv[1] if len(sys.argv) > 1 else "projects/novels1"
sample_size = int(sys.argv[2]) if len(sys.argv) > 2 else 100

novels_dir = Path(project).resolve()
final_dir = novels_dir / "chapters" / "final"
review_dir = novels_dir / "chapters" / "review_final"
review_dir.mkdir(exist_ok=True)

# 获取所有终稿文件
final_files = sorted(final_dir.glob("chapter_*.txt"))
if len(final_files) == 0:
    print("No final chapters found")
    sys.exit(1)

# 随机抽样
random.seed(42)
sample = random.sample(final_files, min(sample_size, len(final_files)))
print(f"Sampling {len(sample)} chapters from {len(final_files)} final chapters")

scores = []
failed = []

for f in sample:
    ch_num = int(f.stem.split("_")[1])
    content = f.read_text(encoding="utf-8")
    word_count = len(content.strip())
    
    # 读取对应的 draft review 作为参考
    draft_review_file = novels_dir / "chapters" / "review" / f"chapter_{ch_num:04d}_review.json"
    draft_score = None
    if draft_review_file.exists():
        try:
            dr = json.loads(draft_review_file.read_text(encoding="utf-8"))
            draft_score = dr.get("overall_score")
        except: pass
    
    # 调用 reviewer 审查终稿
    env = os.environ.copy()
    env["NOVEL_PROJECT_DIR"] = str(novels_dir)
    
    # 使用 reviewer.py 审查指定章节，但需要让它读取 final 而不是 draft
    # 临时方案：直接把终稿内容传给 reviewer 的 stdin 模式（如果有）
    # 否则创建一个临时 draft 文件...
    
    # 简单方案：直接复制 final 到临时 draft 位置，review 后再恢复
    # 但这太复杂了。改用直接调用 mmx-cli 审查
    
    from core.mmx_client import call_mmx
    from core.novel_config import load_config
    
    config = load_config(novels_dir)
    world_file = novels_dir / "world.json"
    world = json.loads(world_file.read_text(encoding="utf-8")) if world_file.exists() else {}
    
    title = world.get("title", "本小说")
    desc = world.get("world_description", "")[:200]
    genre = world.get("genre", "")
    
    system = f"""你是一位资深网络小说编辑，负责对小说章节进行专业质量评估。
本书是《{title}》，世界观背景：{desc}
评分标准：9-10分优秀，8-9分良好，7-8分及格，低于7分需修改，低于5分需重写。
请根据实际质量客观评分，优秀章节完全可以给出9分以上，不要人为压低分数。
输出必须是合法的JSON格式，包含以下字段：
{{
  "overall_score": "请给出0-10的客观评分",
  "verdict": "通过|需修改|需重写",
  "strengths": ["优点1", "优点2"],
  "weaknesses": ["缺点1", "缺点2"],
  "suggestions": "修改建议"
}}"""
    
    prompt = f"""请审查以下第{ch_num}章终稿内容（{word_count}字）：

{content[:8000]}

请给出客观评分和审查意见。"""
    
    try:
        result = call_mmx(system, prompt, max_tokens=4096, temperature=0.3)
        # 提取 JSON
        import re
        json_match = re.search(r'\{.*\}', result, re.DOTALL)
        if json_match:
            review = json.loads(json_match.group())
            score = review.get("overall_score", "N/A")
            verdict = review.get("verdict", "N/A")
            
            # 保存 review
            review_file = review_dir / f"chapter_{ch_num:04d}_review.json"
            review_file.write_text(json.dumps(review, ensure_ascii=False, indent=2), encoding="utf-8")
            
            try:
                s = float(score)
                scores.append(s)
                print(f"  Ch{ch_num}: {s} ({verdict}), draft={draft_score}, words={word_count}")
            except:
                failed.append(ch_num)
                print(f"  Ch{ch_num}: N/A (parse failed), draft={draft_score}")
        else:
            failed.append(ch_num)
            print(f"  Ch{ch_num}: N/A (no JSON), draft={draft_score}")
    except Exception as e:
        failed.append(ch_num)
        print(f"  Ch{ch_num}: ERROR {e}, draft={draft_score}")

if scores:
    avg = sum(scores) / len(scores)
    gte85 = sum(1 for s in scores if s >= 8.5)
    gte8 = sum(1 for s in scores if s >= 8.0)
    lt7 = sum(1 for s in scores if s < 7.0)
    print(f"\n=== Final Sample Review Results ===")
    print(f"Sampled: {len(scores) + len(failed)}")
    print(f"Valid: {len(scores)}, Failed: {len(failed)}")
    print(f"Average: {avg:.2f}")
    print(f">=8.5: {gte85} ({gte85/len(scores)*100:.1f}%)")
    print(f">=8.0: {gte8} ({gte8/len(scores)*100:.1f}%)")
    print(f"<7.0: {lt7} ({lt7/len(scores)*100:.1f}%)")
else:
    print("No valid scores obtained")
