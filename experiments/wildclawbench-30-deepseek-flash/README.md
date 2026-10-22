# WildClawBench 30题：Qwen进化与DeepSeek验收

**`20261013T194546Z`已于纽约时间2026-10-14 15:33:52带缺口结束（finished_with_gaps）。** 六日六夜及S0/S6终点流程均已执行，但保留2项方法部署缺口和5项持久跳过。它与之后的三轮投毒实验独立，不能混用成绩。

## 配置与指标

[配置](config.json)、[任务选择](tasks.json)为只读历史投影，原冻结配置保留在归档。六类各5题，seed42，8个固定用户，9个初始共享技能；本地Qwen3.8-27B执行/进化，DeepSeek Flash普通/视觉评分与A/B比较，本地Omni音频评分。模型和工具链均是论文变体。

六日固定执行180次，终点S0/S6各30次，共240次；Night A/B和预跑另计。逐题使用原时限。付费预算$10；真实密钥只由主机入口读取，不进配置或日志。

| 指标 | 已核实结果 |
| --- | --- |
| 固定执行有效评分 | 239/240；Day5为29/30，缺失保持unknown |
| 终点有效评分与匹配 | 60/60、30/30对 |
| 终点Utility（reward×100） | S0 49.14，S6 51.01，配对均值变化+1.88 |
| 逐题变化 | 8升、7降、15平 |
| 实测墙钟 | Day1首题至终态23.46小时；含预跑23.80小时；终点90.41分钟 |
| 付费结算记录 | 326次；峰时上界$2.079748464/$10，非供应商实际账单 |

3次历史usage缺口按最坏费用$1.0027008计入上界；完整模型调用、token及真实总计算成本未统一核实，记unknown。分数是完成质量，不是整题成功率；本实验没有攻击ASR或conditional persistence。

## 方法差异、异常与浪费

Night1 video-transcribe未加载虽后续修复，历史影响保留；video-frames被同名内置技能覆盖的边界没有在冻结终点修改。验证来源范围中途按授权收窄并记录，不能称全程协议未变。局部A/B接受不保证下日整库收益。

未将旧60题或预跑分数当本次结果。保留部署、验证不可用和任务跳过的完整原因，不用完成流程掩盖方法缺口。旧终点已评一次，本次不追分或重测。

## 证据与入口

- [原实验记录](../../artifacts/30x5-sixcycles-20261016-01/legacy/repository/experiment/reproduction/wildclawbench-30-deepseek-flash/EXPERIMENT.md)、[原冻结协议](../../artifacts/30x5-sixcycles-20261016-01/legacy/repository/experiment/reproduction/wildclawbench-30-deepseek-flash/runtime/runs/20261013T194546Z/frozen-protocol.json)、[最终评分与预算审计](../../artifacts/30x5-sixcycles-20261016-01/legacy/repository/experiment/reproduction/wildclawbench-30-deepseek-flash/runtime/runs/20261013T194546Z/audits/final-grading-budget-audit-20261014.json)、[最终冻结与发布链审计](../../artifacts/30x5-sixcycles-20261016-01/legacy/repository/experiment/reproduction/wildclawbench-30-deepseek-flash/runtime/runs/20261013T194546Z/audits/final-freeze-integrity-audit-20261014.json)。
- [原方法缺口](../../artifacts/30x5-sixcycles-20261016-01/legacy/repository/experiment/reproduction/wildclawbench-30-deepseek-flash/runtime/runs/20261013T194546Z/operations/method-integrity-gaps.json)；[归档根映射](../../artifacts/30x5-sixcycles-20261016-01/archive.json)。
- 新引擎入口为`python -m scripts.wildclawbench --config … --run-id …`；此配置只读，使用`--dry-run`查看配置，不能恢复已完成final。新的科学运行必须另设预算、ID并先过smoke。
