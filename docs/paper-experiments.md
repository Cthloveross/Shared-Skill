# 原始 SkillClaw 实验设置、Benchmark 来源与本地验证

核对日期：2026-10-12。本文专门记录原始 SkillClaw 的正常技能进化实验，以及本项目正常流程的验证范围。依据 [arXiv:2604.08377v2](https://arxiv.org/html/2604.08377v2)，版本日期 2026-08-10。

PoisonedEvolution 的攻击设计、SpreadsheetBench 数据、30/3 批次与 600 次试验单独记录在 [PoisonedEvolution 实验文档](poisoned-evolution-experiments.md)。未说明的参数不能用代码默认值补成论文设置。

## 目录

1. [实验范围](#1-实验范围)
2. [WildClawBench 来源](#2-wildclawbench-来源)
3. [原始 SkillClaw：正常进化实验](#3-原始-skillclaw正常进化实验)
4. [本地验证与论文结果的区分](#4-本地验证与论文结果的区分)
5. [论文没有明确给出的参数](#5-论文没有明确给出的参数)
6. [当前代码与原始论文的差异](#6-当前代码与原始论文的差异)

## 1. 实验范围

原始 SkillClaw 使用 8 个模拟用户，在 WildClawBench 上持续运行 6 天／6 轮：白天执行任务，夜间汇总轨迹、生成候选、验证新旧技能，再把接受的更新提供给下一轮。执行、进化和验证均使用 Qwen3-Max。

这是一段持续使用共享技能库的正常任务实验。与攻击论文的逐项设置对照见 [两种实验的区别](poisoned-evolution-experiments.md#8-与原始-skillclaw-的设置区别)。来源：[SkillClaw §3.2](https://arxiv.org/html/2604.08377v2#S3.SS2)。

WildClawBench 的 60 是任务数；SkillClaw 维护并进化多个 skill，具体被接受的名称见 [§3.4](#34-具体进化了哪些-skill)。论文只报告四个类别，未列出每天执行的 task IDs 和次数，无法确认完整 60 题的实际执行安排。

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

**技能资源也公开了，但 27 份有不同用途。** 上述固定版本的 [WildClawBench skills 目录](https://github.com/InternLM/WildClawBench/tree/8e1fde746584ae1a9c49b70526d49a7a572625cd/skills) 递归包含 27 份 `SKILL.md`：

| 分组 | 数量 | 与 60 题的关系 |
| --- | ---: | --- |
| `03_task1`–`03_task6` | 6 | 六个社交交互任务各自引用一个对应技能 |
| `agent-browser`、`video-frames`、`self-improving-agent-3.0.5` | 3 | 当前任务文件分别在 16、5、1 题中引用，可跨任务复用 |
| `skills/1`–`skills/6` 下的领域技能 | 18 | WildClawBench 自身的 `+Skill` 对比实验资源，每个领域选 3 个；当前 60 题的 `## Skills` 段没有直接引用它们 |
| **公开目录合计** | **27** | **资源文件数，不是 SkillClaw 初始库数量或进化数量** |

18 个领域技能来自 ClawHub，作者在评测时按每个领域下载量选择前三。WildClawBench 的 `+Skill` 实验比较加入领域技能前后的任务成绩，并不执行 SkillClaw 的六轮技能进化。来源：[WildClawBench §4.3](https://arxiv.org/html/2605.10912v1#S4.SS3)、[附录 D](https://arxiv.org/html/2605.10912v1)。

本次逐题核对固定版本的 [60 个任务 Markdown](https://github.com/InternLM/WildClawBench/tree/8e1fde746584ae1a9c49b70526d49a7a572625cd/tasks)：27 题的 `## Skills` 非空，33 题为空；非空段共引用上述 **9 个目录中存在的技能**，另有一题引用 `edge-tts`，该技能不在这 27 份中。这里的“27 题”与“27 份技能”只是数字相同。任务声明也不代表 Agent 实际读取了每个技能；空段不表示任务无法执行。逐题引用清单记录在 [来源清单的 `task_skill_reference_audit`](../reproduction/reference/wildclawbench-source-manifest.json)。

因此，这些资源围绕 Benchmark 的任务或领域准备，但不是 27 份都对应全部 60 题，也不是当前 runner 自动把 27 份全部安装。SkillClaw 历史 runner 按所选任务引用构建初始库，可追加外部库，具体见 [§3.5](#35-公开代码如何准备技能库和任务)。

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

下表是论文表 3 的百分制评分，不能直接当作整题全通过率。表题明确为 **best-skill deployment view**：Day 1 是初始库基线，Day 2–6 展示前夜 validator 决策后保留的最佳已验证技能库，gain 比较 Day 6 与 Day 1。论文没有定义对原始日测分取 running max，不能把历史峰值直接当最终值。来源：[§3.2、表 3](https://arxiv.org/html/2604.08377v2#S3.SS2)。

[表 5、表 7](https://arxiv.org/html/2604.08377v2#S3.T5)另有同库复测读数更高、确认 best-so-far 的记录；论文未给出原始日测／夜测读数或完整选分细则，不能确认每列都是独立、同题全量的新测量。固定官方历史 runner 则明确令 `final.mean_score` 等于最后完成轮的分数，逐题比较 baseline 与 last round，不取跨轮最大值；这是另一时期的实现，不能冒充表 3 的实际产出代码。[历史源码 `61983b0`，第 379–455 行](https://github.com/AMAP-ML/SkillClaw/blob/61983b0788010b3ab0f69899b3e22360758bf55d/skillclaw/experiments/wildclawbench_iterative_evolve.py#L379-L455)。

#### 评价口径：看任务完成效果，技能更新解释改进过程

WildClawBench 每题按多个评分条件得到 `overall_score`（0–1），官方汇总取任务均值。SkillClaw 表 3 展示类别任务成绩；没有给统一的二元任务成功阈值。“完成质量 60 分”不能解释成“60% 的题完全成功”。来源：[WildClawBench §4.1](https://arxiv.org/html/2605.10912v1#S4.SS1)、[固定版本汇总代码](https://github.com/InternLM/WildClawBench/blob/8e1fde746584ae1a9c49b70526d49a7a572625cd/src/utils/grading.py#L309)。

| 口径 | 定义／报告范围 |
| --- | --- |
| 主结果：任务完成质量 | 本地复现为 `100 × mean(overall_score)`，完整阶段要求全部 60 题有效评分；真实失败和超时仍按产物评分 |
| 评分覆盖率 | 有效评分题数 ÷ 应评题数；`completed` 可包含零分，表示执行与评分结束 |
| 满分任务占比 | `score=1` 的任务数 ÷ 应评题数，仅作辅助描述；不是论文定义的整题成功率 |
| 二元整题成功率 | 缺少统一成功条件，当前不报告；不临时设 0.5／0.8 阈值 |
| 技能进化过程 | 表 4–7 有候选、改动摘要、接受／拒绝与后续部署记录；“5 个不同技能”是据表去重统计，并非作者给出的全库覆盖率 |

论文没有完整报告技能库初末数量、逐轮实际使用覆盖率或逐技能前后得分。我们在本地复现中补充真实技能读取、候选决策和版本记录，用来解释任务成绩变化；这些统计不冒称论文原有指标。独立实验流程与实际报告见 [各实验README](../README.md#实验索引)。

| 类别 | Day 1 | Day 2 | Day 3 | Day 4 | Day 5 | Day 6 |
| --- | ---: | ---: | ---: | ---: | ---: | ---: |
| 社交交互 | 54.01% | 60.34% | 60.34% | 60.34% | 60.34% | 60.34% |
| 搜索与检索 | 22.73% | 30.00% | 30.00% | 34.55% | 34.55% | 34.55% |
| 创意生成 | 11.57% | 21.80% | 21.80% | 21.80% | 21.80% | 21.80% |
| 安全与对齐 | 24.00% | 24.00% | 24.00% | 24.00% | 32.00% | 32.00% |

来源：[SkillClaw 表 3](https://arxiv.org/html/2604.08377v2#S3.T3)。这是持续运行中的逐日结果，不是六次独立重复实验。

**表 3 确实报告了进步：四类任务的整体评分都高于 Day 1。** 每一行的 category 是任务类别，分数反映 Agent 使用当天保留的技能库完成该类任务的表现，不是一个技能的分数，也不是该类别技能数量。结合表 4–7，可以分别读取成绩和接受项：

| 任务类别 | Day 1 → Day 6 评分 | 提升（百分点） | 接受文本更新的不同技能名称数 | 该类初始／最终技能总数 |
| --- | --- | ---: | ---: | --- |
| 社交交互 | 54.01% → 60.34% | +6.33 | 1 | 未列完整数量 |
| 搜索与检索 | 22.73% → 34.55% | +11.82 | 1 | 未列完整数量 |
| 创意生成 | 11.57% → 21.80% | +10.23 | 1 | 未列完整数量 |
| 安全与对齐 | 24.00% → 32.00% | +8.00 | 2 | 未列完整数量 |

例如，社交类别表 4 只接受了 `03_task6` 的改写，下一天该类别成绩提高；这支持类别层面的表现改善，不能推出其余社交技能也都修改或变好了。论文 §3.4 列举会议协调、Slack 任务提取、可行性分析、状态汇报、支持分流和管理层总结六种已有社交流程，但没有声明这是完整技能清单，不能把它当作“社交库总共恰好 6 个”的数量设定。来源：[表 4–7](https://arxiv.org/html/2604.08377v2#S3.T4)、[§3.4](https://arxiv.org/html/2604.08377v2#S3.SS4)。

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

### 3.4 具体进化了哪些 skill

原始 SkillClaw 维护一个共享技能库，按不同技能的执行证据提出候选。表 4–7 明确列出 **5 个不同技能名称的更新被接受部署**：

| Skill 名称 | 功能 | 有文本更新被接受的夜晚 |
| --- | --- | --- |
| `03_task6` | 跨部门 Slack 信息汇总、风险识别与管理层简报 | Night 1 |
| `validate-file-existence` | 读取或解析输入前检查文件存在 | Night 1 |
| `validate-tmp-workspace-inputs` | 检查工作目录、输入文件与执行环境 | Night 1 |
| `git-push-with-auth-fallback` | Git 推送失败时提供 patch／bundle 等替代交付 | Night 1、2、3 |
| `git-clone-to-directory` | 正确将仓库克隆到指定目录 | Night 3 |

来源：[表 4](https://arxiv.org/html/2604.08377v2#S3.T4)、[表 5](https://arxiv.org/html/2604.08377v2#S3.T5)、[表 6](https://arxiv.org/html/2604.08377v2#S3.T6)、[表 7](https://arxiv.org/html/2604.08377v2#S3.T7)。这是表中明确记录的接受项，不是完整初始库或最终库的技能总数，也不等于新建 5 个技能；其中包含原有技能的改写以及同一技能在不同夜晚的继续修改。搜索 Night 3、安全 Night 4 是同库复测，没有新技能文本，未计入上表。

会议协调、路径排查、检索规划和多模态流水线等还有其他候选，但表中被拒绝。每天只对实际触发、且有改进潜力的技能提出更新，未必所有技能都修改。任务与技能也不一一对应：一条任务可以调用多个技能，同一技能可以服务多条任务。**论文没有说公开目录中的 27 份都参与或完成了进化；初始库总数、实际提出修改的技能总数，也不能由这 5 个接受项推出。**

论文明确使用 WildClawBench，其完整基准有 60 个任务；SkillClaw 实验只报告四个代表性类别。没有逐用户、逐日的任务清单，不能据此断言每天跑完全部 60 题，或计算为 `60 × 8 × 6` 次执行。来源：[§3.1–3.2](https://arxiv.org/html/2604.08377v2#S3.SS1)。PoisonedEvolution 以 `xlsx` 为单一攻击目标，属于另一份实验设计。

### 3.5 公开代码如何准备技能库和任务

2026-10-12 补查 Git 历史后确认：上游最初公开提交 [`61983b0788010b3ab0f69899b3e22360758bf55d`](https://github.com/AMAP-ML/SkillClaw/tree/61983b0788010b3ab0f69899b3e22360758bf55d)（2026-04-10）包含 WildClawBench 批次、迭代及 Agent 进化实验脚本；它们在 [`f3a23d4e6bfb07b25c024b904bf7766dae6429e9`](https://github.com/AMAP-ML/SkillClaw/commit/f3a23d4e6bfb07b25c024b904bf7766dae6429e9)（2026-04-14）被删除。因此，当前代码没有这些文件，不能推断作者从未公开实验实现。

#### 初始 skill 有代码依据

历史 runner 从运行者提供的 `benchmark_root` 读取 `tasks/` 与 `skills/`：

1. 读取所选任务 Markdown 的 `## Skills` 段，保留在 `benchmark_root/skills` 中存在的技能。
2. 建立本次运行的 `initial_skills/`；若配置 `initial_skills_dir`，先复制这个额外目录。
3. 把所选任务引用的 Benchmark 技能去重复制到初始库；同名文件按代码复制顺序覆盖。

来源：[任务技能解析，第 624 行](https://github.com/AMAP-ML/SkillClaw/blob/61983b0788010b3ab0f69899b3e22360758bf55d/skillclaw/experiments/wildclawbench_batch_evolve_experiment.py#L624)、[初始库构建，第 649 行](https://github.com/AMAP-ML/SkillClaw/blob/61983b0788010b3ab0f69899b3e22360758bf55d/skillclaw/experiments/wildclawbench_batch_evolve_experiment.py#L649)、[迭代 runner 复用初始化，第 79 行](https://github.com/AMAP-ML/SkillClaw/blob/61983b0788010b3ab0f69899b3e22360758bf55d/skillclaw/experiments/wildclawbench_iterative_evolve.py#L79)。代码不会自动下载 Benchmark，资源路径由配置指定。

所以，**有公开技能资源，也有初始库的构造方式**。数量取决于所选任务引用的不同技能，以及是否追加外部库。论文实际运行所用的配置、任务列表、额外技能目录与初始库快照尚未对齐，不能把当前公开目录的 27 份直接写成论文初始库数量。

#### 从公开代码和任务配置可以计算什么

逐题统计固定 Benchmark revision 的 `## Skills`，只计路径存在的不同技能，得到：

| 任务类别 | 当前任务数 | 直接声明且存在文件的不同技能数 | 技能名称 |
| --- | ---: | ---: | --- |
| 生产力工作流 | 10 | 2 | `agent-browser`、`self-improving-agent-3.0.5` |
| 代码任务 | 12 | 0 | 无直接声明 |
| 社交交互 | 6 | 6 | `03_task1`–`03_task6` |
| 搜索与检索 | 11 | 1 | `agent-browser` |
| 创意生成 | 11 | 1 | `video-frames`；另声明但缺少 `edge-tts` 文件 |
| 安全与对齐 | 10 | 0 | 无直接声明 |

这里是**各类任务直接声明的技能并集**，不是六个独立技能库，也不是论文当次各类实际使用数量。`agent-browser` 被两个类别复用，不能把各行相加当全库去重数量。代码会将所选任务的技能放进同一个共享库；代码任务、安全任务声明为 0，不代表运行时看不到共享技能。

本次在空临时目录中执行历史源码的 `_ensure_initial_skills_dir` 方法，并核对复制后的技能文本与源文件完全相同。固定当前公开资源、不追加外部库时：

- 若明确选择表 3 对应四类的全部当前任务，共 38 题，初始共享库为 **8 个**：六个 `03_task*`、`agent-browser`、`video-frames`。
- 若选择全部 60 题，初始共享库为 **9 个**，增加 `self-improving-agent-3.0.5`。

这是**给定条件下由代码算出的初始库**，不是恢复了论文的实际配置；本次只执行资源复制，没有运行 Agent、任务或模型进化。分组及复制验证记录见 [来源清单](../reproduction/reference/wildclawbench-source-manifest.json) 的 `task_skill_reference_audit` 与 `initial_bank_constructor_audit`。

**最终数量需要实际运行产物。** 历史 runner 会保存 `selected_tasks.json`、`initial_skills/` 和每轮 `arms/round-N/after_skills/`，可以据此数技能文件、比较版本与追踪变化。静态代码允许模型改写、创建或跳过，不能预先确定最终几个；共享技能的类别归属还需要任务关联或明确分类规则。来源：[选题清单保存，第 454 行](https://github.com/AMAP-ML/SkillClaw/blob/61983b0788010b3ab0f69899b3e22360758bf55d/skillclaw/experiments/wildclawbench_batch_evolve_experiment.py#L454)、[每轮库快照，第 267 行](https://github.com/AMAP-ML/SkillClaw/blob/61983b0788010b3ab0f69899b3e22360758bf55d/skillclaw/experiments/wildclawbench_iterative_evolve.py#L267)。本次检查已获取的 7 个公开分支、67 个可达提交的文件树，未找到论文当次配置、选题清单或技能快照，不能据此给出论文四类的实际初始／最终数量。

统计时还有一个代码细节：历史报告的 `skills_evolved_this_round` 实际读取整个技能 manifest 的名称列表，manifest 可能已经包含推送的初始技能，因此不能仅凭这个字段就断定所有名称都在当轮修改或新增。应以技能内容差异和实际更新记录核对。来源：[报告字段生成，第 393 行](https://github.com/AMAP-ML/SkillClaw/blob/61983b0788010b3ab0f69899b3e22360758bf55d/skillclaw/experiments/wildclawbench_iterative_evolve.py#L393)、[初始技能推送，第 214 行](https://github.com/AMAP-ML/SkillClaw/blob/61983b0788010b3ab0f69899b3e22360758bf55d/skillclaw/experiments/wildclawbench_iterative_evolve.py#L214)。

#### 任务选择、分配和技能使用

历史代码允许明确给出 `selected_task_files`，或恢复已有 `selected_tasks.json`；否则扫描任务文件，按技能可用性、时限和历史运行信息筛选、排序，默认最多选择 16 题。当配置选择数不少于剩余题数时，会直接全部取用，跳过后续过滤。来源：[选题逻辑，第 575 行](https://github.com/AMAP-ML/SkillClaw/blob/61983b0788010b3ab0f69899b3e22360758bf55d/skillclaw/experiments/wildclawbench_batch_evolve_experiment.py#L575)。

选定任务以 `(task, rollout)` 为单位进入队列，分配给并发 device；一个 device 完成一题后再取下一题。默认每题 rollout 为 1，因此不是每个 device 都运行整套题。来源：[分配模式，第 984 行](https://github.com/AMAP-ML/SkillClaw/blob/61983b0788010b3ab0f69899b3e22360758bf55d/skillclaw/experiments/wildclawbench_batch_evolve_experiment.py#L984)、[任务队列，第 1489 行](https://github.com/AMAP-ML/SkillClaw/blob/61983b0788010b3ab0f69899b3e22360758bf55d/skillclaw/experiments/group_benchmark.py#L1489)。device 是这里的并发执行槽，不能仅凭其数量推成真实用户总数或论文准确部署配置。

执行时先准备任务指定技能，再用当前共享库覆盖同名技能；模型收到可用技能目录，按任务相关性选择并读取技能，日志记录实际读取的名称。任务文件列出的技能用于准备资源，不表示用户随意指定，也不证明 Agent 实际使用了其中每一个。来源：[执行器，第 1207 行](https://github.com/AMAP-ML/SkillClaw/blob/61983b0788010b3ab0f69899b3e22360758bf55d/skillclaw/experiments/wildclawbench_executor.py#L1207)、[技能目录提示，第 557 行](https://github.com/AMAP-ML/SkillClaw/blob/61983b0788010b3ab0f69899b3e22360758bf55d/skillclaw/skill_manager.py#L557)。

#### 历史代码默认值与论文设定分别记录

| 项目 | 历史代码默认值 | 论文正常实验 |
| --- | --- | --- |
| 选题 | 最多 16 题，或配置给出的任务文件 | 来自 WildClawBench，实际任务清单未对齐 |
| 并发 device | 16 | 8 个模拟用户 |
| 迭代进化数 | 3 | 6 天／6 轮 |
| 执行模型 | `kimi-k2.5` | Qwen3-Max |
| Evolver | `gpt-5.4` | Qwen3-Max |
| 初始库 | 所选任务引用的 Benchmark 技能，可追加外部库 | 当次完整清单和版本未对齐 |

来源：[历史配置，第 205 行](https://github.com/AMAP-ML/SkillClaw/blob/61983b0788010b3ab0f69899b3e22360758bf55d/skillclaw/experiments/wildclawbench_batch_evolve_experiment.py#L205)。这些默认值不是论文实际参数。历史迭代 runner 先做 baseline，再执行 N 次“进化 → 评测同一任务集”，因此 N=6 会产生 7 个评测点；不能直接认定它与论文六天表格及夜间接受流程完全相同。来源：[迭代循环，第 86 行](https://github.com/AMAP-ML/SkillClaw/blob/61983b0788010b3ab0f69899b3e22360758bf55d/skillclaw/experiments/wildclawbench_iterative_evolve.py#L86)。

公开技能 bundle、任务 Markdown 与历史实验源码已下载供核查，固定提交、路径和逐文件 SHA256 见 [WildClawBench 来源清单](../reproduction/reference/wildclawbench-source-manifest.json)。大型任务素材和 Docker 镜像未在这次来源核查中下载；这些材料尚未用于运行 Benchmark 或复现论文成绩。

## 4. 本地验证与论文结果的区分

正常工作流smoke、自建工作簿闭环、固定Benchmark任务实验是不同证据层级。自建任务证明执行、收集、进化、发布与复用链路可运行；只有可比任务评分才支持Utility变化。配置、具体模型、有效分母、成本及当前状态集中在[根README的实验索引](../README.md#实验索引)及各实验README。

公开历史runner、服务默认值和论文实际参数分别记录。导入源码、下载技能资源或完成smoke，均不代表对齐原论文当次配置或复现论文成绩。

## 5. 论文没有明确给出的参数

| 参数或材料 | 原始 SkillClaw 说明与缺口 |
| --- | --- |
| 每个用户的任务分配、每日运行次数 | 未给出 |
| 精确任务 ID 与 train/test 划分文件 | 未给出 |
| 论文实际初始 skill 库 | 历史代码给出从 Benchmark 构造的方式，官方技能资源可获取；当次选取、额外库和版本 hash 未对齐，见 §3.5 |
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

来源：[演化服务配置](../evolve_server/core/config.py)、[客户端验证实现](../sharedskill/validation_worker.py)、[周期运行逻辑](../evolve_server/engines/workflow.py)。当前默认分数门槛 0.75 是代码配置，不能写成论文验证门槛。良性工作流smoke与独立实验引擎的真实新旧任务执行对照属于不同验证层，具体启用项由所属配置与证据确认。

组件与部署路径见 [系统架构](architecture.md)；历史项目来源、固定上游 revision 与许可见 [上游说明](upstream.md)。
