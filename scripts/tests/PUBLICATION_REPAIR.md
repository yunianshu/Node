# 发布质量门修复与验证记录

2026-09-30；以修复开始时的实际工作区为基础，保留既有局部修订实现与小说数据。

## 分析、测试设计与复现

基线：有未完成 git am；工作区与索引补丁及所有已修改跟踪文件先备份到系统临时目录，再 am --abort，文件摘要验证无变化。PATH 首位 D:/soft/py/python.exe 缺少 Lib/encodings 与 Lib/os.py，PYTHONHOME/PYTHONPATH 未设置，python -E 仍启动失败。使用已有 Codex Python 3.12.14 创建独立临时验证环境，未修改全局安装或系统环境。

原有68项测试通过，活动 scripts 语法检查通过。代码证据：Reviewer main 未返回失败码；promote 直接复制正文；Polisher失败/降分恢复稿只看历史分数；缓存缺正文摘要；Coordinator 在终稿复检前抽取状态，复检仅告警；旧入口保留独立候选赛马及宽松发布。

先添加 test_publication_contract.py 并运行：32失败、1通过，复现缺失共享AI门、退出码、正文摘要、回退重审和终稿阻断。断言依据本次要求，不通过降低分数、跳过用例或弱化断言修复。

| 验收 | 测试与证据 |
| --- | --- |
| origin两个提取函数 | 实际facts/style输入调用；Writer与Reviewer实际读取origin并进入模型提示词 |
| API/解析/契约与退出码 | 函数CLI与真实HTTP子进程；批量一章缺失即失败；全部失败状态含salvaged |
| 有效低分/高分需修改 | completed返回0，质量门拒绝且无final |
| 三种指纹 | 7.0/8.5/10.0交叉参数，同时验证共享判断与Reviewer实际路径 |
| 正文绑定 | 送审期间修改文件仍记录原快照摘要；旧无摘要、摘要变化强制重审 |
| 回退重审 | Polisher失败、降分、重试耗尽均强制Reviewer重审；不合格不得发布 |
| AI失败 | 发布前异常保留原final；落盘后异常回滚原final并持久化报告门失败 |
| 状态推进 | 终稿复检失败不得调用抽取和进度更新；恢复检测后扫描仍拒绝失败终稿 |
| Coordinator退出码 | Planner、媒体、正文门、整本终审失败均返回1，未发送完成通知 |
| 旧入口 | 根入口及watchdog真实子进程进入Coordinator，失败返回1；参数映射子进程返回17原样传播 |
| 单章冒烟 | 复制隔离样本项目，真实CLI/HTTP固定响应，Writer→Reviewer→final，逐字节核对正文与摘要 |

旧测试 test_keyword_failures_do_not_override_semantic_review 原将AI=0分设为仍通过，与本次明确硬门要求冲突；保留它对人情味、关系、场景观察提示的全部断言，AI硬门由上述拒绝测试单独覆盖。

## 实现

共享AI硬门保留七种原有指纹并补齐三种；报告SHA256基于送审快照；发布核对完整质量条件；历史稿每次恢复重审；失败报告反馈保留契约错误、门原因和修订任务；终稿复检移至状态更新前并持久化失败。旧入口仅转发，明确拒绝无等价映射参数。README与novel-gen说明按实际默认配置同步。

## 本地验证

```powershell
$env:PYTHONPATH="<repo>/scripts"
python -m pytest tests scripts/tests -q
python -m compileall -q scripts
python scripts/pipeline/coordinator.py --help
python _gen_serial.py --help
python scripts/maintenance/gen_serial_watchdog.py --help
git diff --check
```

真实模型验证尝试只在原小说的完整复制项目内运行，清空webhook、禁用媒体和背景监控。原项目所有文件SHA256前后相同。

| Agent | 实际provider/model | 凭据 |
| --- | --- | --- |
| Outliner / Writer | zhipu / glm-4.6 | 缺失 |
| Outline Reviewer / Reviewer | zhipu / glm-4.5-flash | 缺失 |

复制项目Reviewer第2章：退出1、报告failed、无final；Coordinator第7章单章：退出1、正文报告未生成、无final。缺少凭据，未完成真实模型生成质量验证。固定HTTP响应冒烟仅证明流程正确，不能替代真实模型验证。

最终完整测试：139通过（17.17秒）；56个Python文件逐个py_compile通过；Coordinator、根兼容入口和watchdog的--help均返回0；git diff --check通过。未修改原项目数据，不强推。
