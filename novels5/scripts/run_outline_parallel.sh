#!/bin/bash
cd "D:/AiProject/Node/novels5/scripts"

# 启动5个并发的planner，每批100章
for start in 101 201 301 401 501; do
    end=$((start + 99))
    python -u planner.py --start $start --end $end --outline-file "D:/AiProject/Node/novels5/outline_part_$(printf "%04d" $start)_$(printf "%04d" $end).json" > "D:/AiProject/Node/novels5/logs/planner_${start}_${end}.log" 2>&1 &
done
wait
echo "第一批5个任务完成"
