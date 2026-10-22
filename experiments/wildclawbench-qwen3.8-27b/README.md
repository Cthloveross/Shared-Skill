# WildClawBench全本地60题历史实验

**本目录没有完整六轮结果。** v1因入口协议不一致停止，v2因GPU调度暂停；两者均没有S0/S6终点结果。后续独立30题实验的成绩不得补入本目录。

## 协议与配置

[只读配置](config.json)保留历史设计：60题、8个固定用户、9个初始共享技能、六日六夜与S0/S6终点，共480次固定执行，预跑和Night验证另计。执行、进化和普通/视觉评分为本地Qwen3.8-27B，音频和图像使用本地后端；收费API上限$0。

同题每Day执行一次，用户数不是任务重复倍数。旧库/候选库在完整工具环境验证，通过本地接受规则后发布。reward均值×100为质量指标；基础设施和评分缺失保持unknown，不补零。模型、OCR变体和工具链不同于论文原始配置。

## 实际状态与结果

| 运行 | 状态 | 有效范围 |
| --- | --- | --- |
| v1 20261012T230206Z | superseded_protocol_mismatch | Day1–3各60题完成，Night1–2发布，Night3中止；Day均分49.16→47.75→43.66 |
| v2 20261013T185308Z | paused_for_gpu_scheduling | 8/8预跑及Day1的10/60有效评分，共18份记录；无Night1或终点 |

v1的执行入口遗漏已确认，分数仅作诊断，不能视为修复后的六轮收益。v2从S0独立开始，不复用v1分数，2026-10-13 15:39 EDT暂停给独立30题实验让出GPU；打开文档或聊天不会自动续跑。

尚无最终Utility变化、行为ASR或conditional persistence。完整可比墙钟、总模型调用/token和计算成本未核实，记unknown；配置$0不是实际计算成本为0。历史监督器跳过失败项、保留原库推进等授权另有证据，不能把带缺口流程写成原完整协议完成。

## 证据与入口

- [原实验记录](../../artifacts/30x5-sixcycles-20261016-01/legacy/repository/experiment/reproduction/wildclawbench-qwen3.8-27b/EXPERIMENT.md)、[v1冻结协议](../../artifacts/30x5-sixcycles-20261016-01/legacy/repository/experiment/reproduction/wildclawbench-qwen3.8-27b/runtime/runs/20261012T230206Z/frozen-protocol.json)、[v1停机原因](../../artifacts/30x5-sixcycles-20261016-01/legacy/repository/experiment/reproduction/wildclawbench-qwen3.8-27b/runtime/runs/20261012T230206Z/operations/entry-alignment-supersession.json)、[v2冻结协议](../../artifacts/30x5-sixcycles-20261016-01/legacy/repository/experiment/reproduction/wildclawbench-qwen3.8-27b/runtime/runs/20261013T185308Z/frozen-protocol.json)、[v2暂停记录](../../artifacts/30x5-sixcycles-20261016-01/legacy/repository/experiment/reproduction/wildclawbench-qwen3.8-27b/runtime/runs/20261013T185308Z/operations/gpu-scheduling-pause/pause.json)。
- [归档根映射](../../artifacts/30x5-sixcycles-20261016-01/archive.json)解析历史路径；不改写冻结JSON或重标旧分数。
- 通用维护代码已移到scripts/wildclawbench；此配置只读，当前工作不恢复该历史运行。未来运行需新ID、有效预算和真实smoke。
