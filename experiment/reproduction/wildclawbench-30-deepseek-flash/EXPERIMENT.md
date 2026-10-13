# WildClawBench 30 题：本地 Qwen 六轮进化、DeepSeek 验收

## 目的和解释边界

本实验测量共享技能更新后**任务完成质量**是否提高：六个白天的平均任务分，以及相同评分后端下终点 S6 与 S0 的逐题差值。平均分不是二元任务成功率；同时报告满分、零分、超时、工具错误、技能实际读取和候选接受／拒绝数量。新增或修改技能的数量属于过程指标。

这是 SkillClaw 方法的 **30 题缩减实验及验收后端变更**，不是论文全部实验的严格复刻。与旧运行同时存在入口修复、任务子集和裁判差异，因此不能把两次运行分数的差别单独归因于换裁判；本实验以自己的 Day1 和 S0/S6 对照判断进步。执行器、进化器仍为本地 Qwen；DeepSeek 只负责普通／视觉评分和 A/B 结果比较。规则、文件、模拟服务检查保持官方代码；音频评分仍用本地 Omni。不能先验保证独立裁判会使成绩上涨。

[论文 §3.2](https://arxiv.org/html/2604.08377v2#S3.SS2)的执行、进化、验证模型为 Qwen3-Max，用户数 8、轮数 6，使用完整 60 题；本实验改为 30 题、本地 Qwen3.8-27B 执行／进化和 DeepSeek 验收。官方基准检查含规则与不同 LLM 调用，论文未逐项独立列出全部评分后端。浏览器免费搜索、Omni／SDXL 和私有 Docker 环境也属于本地工具链差异，不能只写“换了模型”。

## 固定设计

| 项目 | 本实验 |
|---|---|
| 官方任务池 | WildClawBench 60 题；代码 revision `8e1fde746584ae1a9c49b70526d49a7a572625cd` |
| 数据 revision | `75f945578aa00cbdb8f46e4d42e4f4e98f704b4f` |
| 选择 | 六类各 5 题；seed=42；类别和题号排序后依次 `random.Random(42).sample(group,5)`；不读取成绩 |
| 主模型和进化器 | GPU1 Qwen3.8-27B；revision `1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0` |
| 普通／视觉裁判 | 官方 DeepSeek API `deepseek-flash`；2026-10-13 文档标记为 DeepSeek-V4.1-Flash |
| 模拟用户 | 固定 8 个身份；按排序后的 30 题轮流分配，6 人各 4 题、2 人各 3 题 |
| 初始库 | 原始 9 个公开技能完整 bundle；不继承旧实验更新；S0 hash `083b44f303788aad687a6fdc4e85bfc3f47d049844f20782ba4d9c3b95aa5b01` |
| 轮数 | Day1 使用 S0，Night1 发布 S1；依此六日六夜得到 S6 |
| 终点 | S0 和 S6 各执行同一 30 题，不再进化；每题随机化两个分支执行先后 |
| 固定执行量 | 6×30 + 2×30 = **240**；预跑和夜间 A/B 另计 |
| 付费上限 | **10 美元**；按最高峰时费率和并发预留保守核算 |

[任务清单](tasks.json)保留原题文件 hash、分类、时限及用户分配；[配置](config.json)记录全部接口和参数。此次随机子集没有 OCR 02.06，30 道题均保留原始题目提示。任务原生时限逐题使用，官方执行前缀逐字对齐，没有统一 300 秒限制。

用户是固定身份和分配，不是另外一个生成任务的模型；每轮每题由一个用户执行一次。用户共享当日冻结技能库，每题恢复干净环境，跨轮只继承技能。技能选择由 Agent 根据任务进行，记录实际读取；并非每题强制读取技能，也不保证 9 个技能都会更新。官方镜像自带的技能仍作为固定环境存在（库存 53 个目录、当前配置下 19 个可用目录项，另有 3 个工具 helper）；它们和这 9 个共享技能分别记录，不进入共享技能进化。

## 模型、接口和费用

Qwen 参数保持原对照一致：thinking、medium effort、temperature=1、top_p=0.95、top_k=20、min_p=0、presence_penalty=0、repetition_penalty=1、seed=42、max_tokens=16384、context=131072、BF16、TP=1、vLLM max_num_seqs=8。

DeepSeek 裁判固定 thinking enabled、high effort、top_p=0.95、max_tokens=16384。Qwen 专有参数不转发；裁判不调用工具。API 模型别名不能固定权重 revision，因此保存每次请求、有效参数、响应 model 字段及时间。官方说明 medium 实际映射 high；参数差异与模型差异都属于本实验协议。

| 路由 | 后端 |
|---|---|
| `19182/runs/<id>/v1` | Qwen 主模型／进化，音频转 Omni、图像生成转 SDXL |
| `19182/runs/<id>/judge/v1` | 普通／视觉评分和 A/B 比较转 DeepSeek；含音频输入仍转本地 Omni |
| Qwen / Omni / SDXL | 本机 `18181 / 18183 / 18184` |

真实 key 只由主机 router 从未跟踪的 `key.env` 中读取变量 `DS`，不写入容器、实验配置、源码或请求日志。API 评分在预跑、六天、A/B 和终点全部使用同一路由。服务失败标记 pending_score，不作为实测零分；容错跳过项单列，缺失成绩的阶段不能声称完成。

[官方价格](https://api-docs.deepseek.com/quick_start/pricing/)和[thinking 参数](https://api-docs.deepseek.com/guides/thinking_mode/)于 2026-10-13 核对。低峰每百万 token 为 cache hit $0.003、miss $0.15、output $0.60；峰时为两倍。本地账本始终按峰时上界扣预算，不冒称实际账单。请求前以完整上下文上限及固定输出预算预留并发费用；完成后用实际 usage 释放差额。缺失 usage 保留最坏预留费用，router 重启不会清零。

[费用估计](budget-estimate.json)基于旧 Qwen 请求量，评分和比较预计约 $0.5–1，实际调用量、视觉和 tokenizer 存在差异；预算上限仍为 $10。没有把 Agent 或进化调用转成收费 API。

## 夜间候选和发布

仅当日正式轨迹进入进化器，包含成功与失败；预跑、验证、终点轨迹不进入。复用原生 Agent Evolver，允许修改、新建或跳过。

每个候选对所有来源相关任务做真实旧库／候选库 A/B，各自恢复相同初始素材和模拟状态，使用同一 Qwen 和完整工具链。候选逐个处理，前一接受项成为下一验证的工作库。要求相关任务逐题不退分、DeepSeek 比较支持候选，且总成绩提高，或成绩持平但工具错误／超时减少；完全平局拒绝。此规则是明确化的本地操作定义，论文未公开这些数值阈值。

现有接受规则不增加新的耗时裕量或重复采样门槛。分数改善分支可能同时增加执行时间；通过一次局部 A/B 不能保证下一天全体成绩提高。对这些风险保留完整验证、时长和错误数据，避免把裁判更换误认为已经解决所有退化。

夜间结束原子发布候选确切文件和 hash，之后不再模型合并。白天技能库冻结，拒绝项不部署。恢复按任务、阶段、候选、分支、输入、协议及技能库 hash 匹配。

## 环境和启动

本实验结果和私有 Docker daemon 存入本目录 `runtime/`，复用旧实验已校验的只读任务素材与官方镜像 tar；不复用成绩。GPU1 共用 Qwen、Omni 和 SDXL，辅助服务串行调度。旧 60 题 v2 已保存后暂停，让本实验独占实验请求负载。旧运行 `20261013T185308Z` 的 18 条结果（8 预跑、10 Day1）、冻结源码和 7 个未完成任务容器已保留；没有将这批成绩用于本实验。暂停审计在旧运行 `operations/gpu-scheduling-pause/`。

科学代码来自独立 worktree `/home/tc442/projects/Shared-Skill-api`，分支 `experiment/wildclawbench-30-api`、提交 `e15b97e`；主分支旧实验冻结代码不被覆盖。运行时冻结全部核心、进化提示、本目录入口和配置；源码快照与 Git revision 写入 runtime。若移动目录，先按配置建立对应 worktree，再开始新的运行，不能改现有运行的配置后复用旧成绩。

```bash
.venv/bin/python experiment/reproduction/wildclawbench-30-deepseek-flash/run.py prepare
.venv/bin/python experiment/reproduction/wildclawbench-30-deepseek-flash/run.py pilot
.venv/bin/python experiment/reproduction/wildclawbench-30-deepseek-flash/run.py run
.venv/bin/python experiment/reproduction/wildclawbench-30-deepseek-flash/run.py resume
.venv/bin/python experiment/reproduction/wildclawbench-30-deepseek-flash/run.py status
.venv/bin/python experiment/reproduction/wildclawbench-30-deepseek-flash/run.py report
```

正式启动自动先执行六类预跑并核对文字、工具、图片、音频和生成接口。预跑使用独立阶段，结束后每题重建环境，正式 Day1 从 S0 开始。无人值守监督器保留原生重试，错误耗尽后记录缺失和跳过，阻止无有效 A/B 证据的候选被发布；不中途伪造分数或改变门槛。

## 耗时估计和进度

旧日志重排至此次 30 题、8 个固定用户后：一批约 44–50 分钟，含历史重试可达 64 分钟；固定 240 次约 6–8 小时。夜间候选逐个验证，两臂依次运行，加生成约 3.2–4 小时／夜。正式部分预计 **25–32 小时**，含预跑、准备和异常排期 **26–36 小时**。详见 [timing-estimate.json](timing-estimate.json)，不是简单把累计任务时间除以 8。

API 只替换评分，旧日志中扣除评分的理论夜间省时约 9 分钟／夜，还需加入 API 实际响应时间；它不会使本地任务执行本身加速。真实预跑与 Night1 完成后更新 ETA。

2026-10-13 15:35 纽约时间：DeepSeek 文字／视觉实测均 HTTP 200，分别约 0.9／1.3 秒；本地三类服务 ready。正在完成正式启动前的协议核验和独立环境准备，尚无 Day1 新实验成绩。

详细产物：`runtime/runs/<run_id>/frozen-protocol.json`、`startup-inventory.json`、`events.jsonl`、`state.json`、`banks/S0..S6`、`executions/`、`nights/`、`report/`；API 请求位于 `runtime/api-evidence/`，预算位于 `runtime/paid-api/budget.json`。阶段和分类 CSV、曲线、逐题终点差值以及异常汇总由 report 生成。只有六天、六夜和终点全部完成，才报告完整实验结果。

启动前核验：146 个测试、213 个子测试通过；独立审计逐题核对了所选 30 题中 21 题的 LLM 评分路由，未发现付费评分遗漏。DeepSeek 从官方任务镜像经主机 router 的连通性另有 `runtime/api-container-preflight.json`。

容器连通性已 HTTP 200 实测通过。临时手工预检首次继承镜像默认代理而等待，清除代理后正常；正式任务和进化入口本来就清除代理，因此没有为此改变科学代码。


正式入口已于 **2026-10-13 15:45:46（纽约时间）** 启动，运行 `20261013T194546Z`。六种本地接口检查全部 HTTP 200；正在执行六题预跑，通过后自动开始 Day1。科学协议 hash 为 `e24e2eab494aa382185bb305a837441682768c6dd3fc05e5f3ff84ae5b0f2724`，冻结 72 份源码，独立控制器及无人值守监督器已启动。

[本次运行报告](runtime/runs/20261013T194546Z/report/REPORT.md)持续生成；源码压缩快照在 `runtime/runs/20261013T194546Z/operations/frozen-source.tar.gz`。准备完成、接口检查通过和预跑开始均不等于正式六轮完成。

<!-- LIVE_STATUS -->
## 当前运行记录

- 状态：`pilot`。
- 运行：`runtime/runs/20261013T194546Z/`。
- 报告更新：`2026-10-13T19:46:47.991948+00:00`；阶段更新：`2026-10-13T19:45:56.383722+00:00`。
- 已记录任务执行：0；终点有效配对：0/30。
- [运行报告](runtime/runs/20261013T194546Z/report/REPORT.md)。
