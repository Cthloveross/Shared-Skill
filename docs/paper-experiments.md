# 原始 SkillClaw 实验设置、Benchmark 来源与本地验证

核对日期：2026-10-12。本文专门记录原始 SkillClaw 的正常技能进化实验，以及本项目正常流程的验证范围。依据 [arXiv:2604.08377v2](https://arxiv.org/html/2604.08377v2)，版本日期 2026-08-10。

PoisonedEvolution 的攻击设计、SpreadsheetBench 数据、30/3 批次与 600 次试验单独记录在 [PoisonedEvolution 实验文档](poisoned-evolution-experiments.md)。未说明的参数不能用代码默认值补成论文设置。

## 目录

1. [实验范围](#1-实验范围)
2. [WildClawBench 来源](#2-wildclawbench-来源)
3. [原始 SkillClaw：正常进化实验](#3-原始-skillclaw正常进化实验)
4. [本地正常闭环的验证结果](#4-本地正常闭环的验证结果)
5. [论文没有明确给出的参数](#5-论文没有明确给出的参数)
6. [当前代码与原始论文的差异](#6-当前代码与原始论文的差异)

## 1. 实验范围

原始 SkillClaw 使用 8 个模拟用户，在 WildClawBench 上持续运行 6 天／6 轮：白天执行任务，夜间汇总轨迹、生成候选、验证新旧技能，再把接受的更新提供给下一轮。执行、进化和验证均使用 Qwen3-Max。

这是一段持续使用共享技能库的正常任务实验。与攻击论文的逐项设置对照见 [两种实验的区别](poisoned-evolution-experiments.md#8-与原始-skillclaw-的设置区别)。来源：[SkillClaw §3.2](https://arxiv.org/html/2604.08377v2#S3.SS2)。

## 2. WildClawBench 来源

WildClawBench 是上海人工智能实验室与合作高校的 Shuangrui Ding、Xuanlang Dai、Long Xing 等作者发布的独立基准。它由研究者人工编写现实工作场景任务，覆盖完整 Agent 工具执行。来源是 [Benchmark 论文](https://arxiv.org/abs/2605.10912) 与 [InternLM 官方仓库](https://github.com/InternLM/WildClawBench)。

| 内容 | 官方入口 |
| --- | --- |
| 任务规范、评分代码、运行框架 | [InternLM/WildClawBench](https://github.com/InternLM/WildClawBench) |
| 项目介绍 | [WildClawBench 项目页](https://internlm.github.io/WildClawBench/) |
| 工作目录素材、任务资源与 Docker 镜像 | [internlm/WildClawBench 数据仓库](https://huggingface.co/datasets/internlm/WildClawBench) |
| Harbor 格式资源 | [WildClawBench-Harbor](https://huggingface.co/datasets/internlm/WildClawBench-Harbor) |
| Benchmark 自身的公开执行轨迹 | [WildClawBench-Trajectories](https://huggingface.co/datasets/internlm/WildClawBench-Trajectories) |

一题包含任务提示、Markdown 规范及 YAML 元数据、输入素材、环境准备和评分逻辑。Agent 在 Docker 工作目录中调用工具执行任务，评分结合产物检查、环境状态审计和模型语义判断。部分交互任务有模拟参与者，因此不能把所有外部交互都理解成真实用户在线参与。

| 任务类别 | 数量 |
| --- | ---: |
| Productivity Flow：生产力工作流 | 10 |
| Code Intelligence：代码任务 | 12 |
| Social Interaction：社交交互 | 6 |
| Search & Retrieval：搜索与检索 | 11 |
| Creative Synthesis：创意生成 | 11 |
| Safety Alignment：安全与对齐 | 10 |
| **合计** | **60** |

60 题中，36 题英文、24 题中文；26 题多模态、34 题纯文本。来源：[Benchmark §3](https://arxiv.org/html/2605.10912v1#S3)。

版本边界：本次核对的官方代码 revision 为 `8e1fde746584ae1a9c49b70526d49a7a572625cd`。当前资源支持多种 Agent 框架，并持续更新；这个 revision 和公开轨迹尚未被确认与 SkillClaw 的 8 用户、6 轮实验环境及日志完全一致。

Benchmark 自身的评测协议给出单题 300–1200 秒预算和 GPT-5.4 评分模型；原始 SkillClaw 则写明全流程使用 Qwen3-Max。前者的预算、评分配置不能直接填入后者的实验参数。来源：[Benchmark §3.2](https://arxiv.org/html/2605.10912v1#S3.SS2)、[§4.1](https://arxiv.org/html/2605.10912v1#S4.SS1)。

## 3. 原始 SkillClaw：正常进化实验

### 3.1 主要参数

| 参数 | 论文设定 | 原文位置 |
| --- | --- | --- |
| Agent | OpenClaw | §3.2 |
| 用户 | 8 个模拟并发用户，各自根据目标和任务要求使用 Agent | §3.2 |
| 共享对象 | 当前经过验证的最佳 skill 库 | §3.2 |
| 初始状态 | 第 1 天使用初始 skill 集合，作为 baseline | §3.2 |
| 模型 | 执行、进化、验证均用 Qwen3-Max | §3.2 |
| 主实验长度 | 6 天、6 轮 | §3.2 |
| 每轮阶段 | 白天执行任务；夜间进化、验证 | §3.2 |
| 候选范围 | 当天被使用、且有改进潜力的 skill | §3.2 |
| 进化动作 | 改进已有 skill、创建新 skill、跳过 | §2.2 |
| 验证任务 | 从当天交互数据选择相关任务 | §2.3 |
| 验证环境 | 空闲的用户环境；新旧版本在同一环境使用完整工具链执行 | §2.3 |
| 接受标准 | 根据任务成功和执行稳定性比较，候选优于当前版本才接受 | §2.3、§3.2 |
| 同步时机 | 接受的更新进入共享库，供下一天使用 | §2.3 |
| 报告范围 | WildClawBench 的 4 个代表性类别 | §3.2、表 3 |
| 结束条件 | 主实验固定到第 6 轮结束，没有明确的自动收敛或提前停止规则 | §3.2 |

来源：[方法 §2](https://arxiv.org/html/2604.08377v2#S2)、[实验 §3.2](https://arxiv.org/html/2604.08377v2#S3.SS2)。这里的进化主要修改技能文件和可复用流程，不是训练模型权重。

### 3.2 给定一个 skill，后续如何运行

1. **使用**：不同用户的 Agent 使用同一个共享库，根据各自任务选择 skill。
2. **留存证据**：记录请求、工具动作、错误、恢复过程和最终结果。
3. **聚合**：把引用同一 skill 的会话归为一组，同时看成功与失败记录；没用到 skill 的会话另行用于发现缺失能力。
4. **生成候选**：模型改写技能、创建技能，或认为证据不足而跳过。
5. **执行验证**：相关任务分别用当前版本和候选版本完整执行，再比较结果。
6. **部署**：接受的候选进入共享库；拒绝的候选不替换当前版本。
7. **下一轮使用**：用户继续提交自己的任务，新的执行记录进入下一轮。

论文没有说明把同一个 skill 的不同阶段任务预先分配给不同用户；用户共享的是技能库和经验。8 个用户的具体任务安排与每日执行次数未给出，不能算成 `60 × 8 × 6` 次任务运行。来源：[§2.1–2.3、§3.2](https://arxiv.org/html/2604.08377v2#S2)。

附录提示词另规定摘要约 8–15 句、部分轨迹字段值约截断到 400 字符。这些是证据格式约束，不是模型推理 token 预算。见 [SkillClaw v2 PDF 附录](https://arxiv.org/pdf/2604.08377v2)。

### 3.3 论文报告的 6 天结果

下表是论文表 3 的百分制评分，不能直接当作整题全通过率。第 1 天为初始库基线，第 2–6 天使用前一夜验证后保留的技能库。

| 类别 | Day 1 | Day 2 | Day 3 | Day 4 | Day 5 | Day 6 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 社交交互 | 54.01% | 60.34% | 60.34% | 60.34% | 60.34% | 60.34% |
| 搜索与检索 | 22.73% | 30.00% | 30.00% | 34.55% | 34.55% | 34.55% |
| 创意生成 | 11.57% | 21.80% | 21.80% | 21.80% | 21.80% | 21.80% |
| 安全与对齐 | 24.00% | 24.00% | 24.00% | 24.00% | 32.00% | 32.00% |

来源：[SkillClaw 表 3](https://arxiv.org/html/2604.08377v2#S3.T3)。这是持续运行中的逐日结果，不是六次独立重复实验。

| 类别 | Night 1 | Night 2 | Night 3 | Night 4 | Night 5 | Night 6 |
| --- | --- | --- | --- | --- | --- | --- |
| 社交交互 | 接受 | 拒绝／同库复测 | 拒绝 | 拒绝／同库复测 | 拒绝／同库复测 | 拒绝 |
| 搜索与检索 | 接受 | 拒绝 | 接受／同库复测 | 拒绝 | 拒绝 | 拒绝 |
| 创意生成 | 接受 | 拒绝 | 拒绝 | 拒绝 | 拒绝 | 拒绝 |
| 安全与对齐 | 接受 | 接受 | 接受 | 接受／同库复测 | 拒绝 | 拒绝 |

来源：[表 4–7](https://arxiv.org/html/2604.08377v2#S3.T4)。这里是类别的夜间决策摘要，不能把每个单元格都当作一次实际文本更新。搜索类别的 Night 3、安全类别的 Night 4 没有新 skill 文本，但同库复测得到更高成绩。因此不能将每次涨分都归因于 skill 修改，也不能把作者关于部署单调改进的表述理解成所有未来任务都保证不退化。

论文还报告一个独立的单轮受控实验，使用 3 个自定义查询：

| 查询 | 更新前 | 更新后 |
| --- | ---: | ---: |
| 基本信息提取 | 21.7% | 69.6% |
| 截止时间解析 | 41.1% | 48.0% |
| 保存报告 | 28.3% | 100.0% |
| 平均 | 30.4% | 72.5% |

来源：[§3.4、表 8](https://arxiv.org/html/2604.08377v2#S3.T8)。这个实验单独验证常见流程缺陷，不能替代完整的 6 轮主实验。

## 4. 本地正常闭环的验证结果

| 项目 | 本地已验证内容 |
| --- | --- |
| 模型 | `gpt-5.6-sol`，通过 Codex CLI 桥接访问 |
| 输入 | 本项目生成的 Sales、Expenses、Inventory 三个真实 XLSX 工作簿 |
| 旧版执行 | 前两题各出现 1 次冻结表头验证错误，修复后通过 |
| 进化输入 | 前两题真实执行和工具反馈记录 |
| 进化输出 | 增加提前设置 `freeze_panes=A2` 的步骤，版本 v1 → v2 |
| 启用阶段 | 摘要、session judge、evolver、merge、skill verifier、版本登记与发布 |
| 验证与发布 | LLM skill verifier 接受，分数 0.92；local 存储、direct 发布 |
| 后续复用 | 第三题实际读取发布版本，内容 hash 一致；第一次验证通过，0 次验证错误 |
| 模型调用数 | 29 次 |
| 实际进化轮数 | 1 轮 |

证据见 [良性验收说明](../benign_validation/README.md)；本地详细证据位于 `benign_validation/results/gpt56-e2e-20261011-v2/` 下的 `run.json`、`verification.json`、工作簿和执行日志。

这个实验验证了“执行 → 收集 → 进化 → 发布 → 后续复用”，没有完成论文的 8 用户、6 轮实验。三个工作簿是自建验收样例；没有在固定任务集上对新旧版本进行完整对照，也没有测量 WildClawBench 分数。公式检查仅验证保存的字符串和数据／样式保留，没有重算公式。桥接将消息角色转换为文本，不等价于原生 Chat Completions 消息处理。

### 尚未完成的正常实验

论文的 8 用户、6 轮 WildClawBench 实验，以及在相关任务上完整执行新旧技能的验证对照，尚未完成。此次验证限定到一个 xlsx skill 的三个自建任务，不能据此确认全部 benign skill 都可用。投毒机制实验与参考数据准备情况分别见 [PoisonedEvolution 专篇](poisoned-evolution-experiments.md#10-本地数据与复现进度)。

## 5. 论文没有明确给出的参数

| 参数或材料 | 原始 SkillClaw 说明与缺口 |
| --- | --- |
| 每个用户的任务分配、每日运行次数 | 未给出 |
| 精确任务 ID 与 train/test 划分文件 | 未给出 |
| 初始 skill 内容、来源、版本 hash | 未完整给出 |
| 模型精确快照、服务端版本 | 未给出可完全固定的版本信息 |
| 温度、top-p、随机种子、推理 token 上限 | 未明确给出 |
| 验证任务列表、复测次数、量化改进门槛 | 未给出 |
| 自动收敛／提前停止规则 | 未说明；固定 6 轮 |
| 完整实验日志、逐次版本与总体 token 开销 | 未在核对材料中建立对应关系 |
| 模型部署硬件与 GPU 配置 | 未明确给出 |

“未给出”针对本次核对的原始 SkillClaw v2 及官方材料。自行选定的任务划分、温度、token 限额、seed 和验证门槛，应单独记录为本地参数。投毒论文的缺失项见 [专篇](poisoned-evolution-experiments.md#9-论文没有明确给出的复现参数)。

## 6. 当前代码与原始论文的差异

| 项目 | 原始论文描述 | 当前仓库实现 |
| --- | --- | --- |
| 候选发布 | 真实执行新旧版本，候选更好才部署 | 默认 `publish_mode=direct`，默认关闭 `use_skill_verifier` |
| 可选 skill verifier | 论文描述完整执行后比较的 validator，未定义这一可选文本评审组件 | LLM 根据技能内容与记录做质量评审 |
| 客户端 replay 验证 | 同一环境、完整工具链执行任务 | 当前 replay 分支生成文本答复，由 PRM 评分 |
| replay 接受条件 | 候选优于当前版本 | 当前代码允许候选均分等于 baseline，且达到分数门槛 |
| 持续服务停止 | 论文实验固定 6 轮 | 周期服务持续处理新记录，没有自动收敛停止条件 |

来源：[演化服务配置](../evolve_server/core/config.py)、[客户端验证实现](../sharedskill/validation_worker.py)、[周期运行逻辑](../evolve_server/engines/workflow.py)。当前默认分数门槛 0.75 是代码配置，不能写成论文验证门槛。我们的良性运行主动启用了 skill verifier，但没有运行论文描述的真实新旧任务执行对照。

组件与部署路径见 [系统架构](architecture.md)；历史项目来源、固定上游 revision 与许可见 [上游说明](upstream.md)。
