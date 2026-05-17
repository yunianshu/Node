#!/usr/bin/env python3
"""删除低分章节的初稿和审查报告，以便重新生成"""
import json
import os
from pathlib import Path

NOVELS_DIR = Path("D:/AiProject/Node/novels4")
DRAFT_DIR = NOVELS_DIR / "chapters" / "draft"
REVIEWS_DIR = NOVELS_DIR / "reviews"
FINAL_DIR = NOVELS_DIR / "chapters" / "final"

THRESHOLD = 8.0

def main():
    deleted_draft = 0
    deleted_review = 0
    deleted_final = 0
    chapters = []

    for i in range(1, 2001):
        review_file = REVIEWS_DIR / f"chapter_{i:04d}_review.json"
        if not review_file.exists():
            continue
        try:
            data = json.load(open(review_file, encoding="utf-8"))
            score = data.get("overall_score")
            if score is not None and float(score) < THRESHOLD:
                chapters.append((i, float(score)))
                # 删除draft
                draft_file = DRAFT_DIR / f"chapter_{i:04d}.txt"
                if draft_file.exists():
                    draft_file.unlink()
                    deleted_draft += 1
                # 删除review
                review_file.unlink()
                deleted_review += 1
                # 删除final
                final_file = FINAL_DIR / f"chapter_{i:04d}.txt"
                if final_file.exists():
                    final_file.unlink()
                    deleted_final += 1
        except Exception as e:
            print(f"第{i}章处理失败: {e}")

    print(f"共找到 {len(chapters)} 章评分 < {THRESHOLD}")
    print(f"删除初稿: {deleted_draft}")
    print(f"删除审查: {deleted_review}")
    print(f"删除终稿: {deleted_final}")

    # 保存列表
    with open(NOVELS_DIR / "rewrite_list.json", "w", encoding="utf-8") as f:
        json.dump([{"chapter": c, "score": s} for c, s in chapters], f, ensure_ascii=False, indent=2)

    print(f"章节列表已保存到 rewrite_list.json")

if __name__ == "__main__":
    main()
