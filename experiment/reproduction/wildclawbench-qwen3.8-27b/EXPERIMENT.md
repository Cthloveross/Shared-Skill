# Qwen3.8-27B 全本地六轮技能进化实验

## 1. 实验身份和完成标准

这是 **SkillClaw 方法的本地模型复现**，研究问题是：同一批 60 个真实工具任务，经 8 个模拟用户的运行证据和六晚共享技能更新后，任务成绩改善多少？不能直接把本实验数字称为原论文 Qwen3-Max 表 3 的复现数字。

只有六个白天、六个夜晚、最终 S0/S6 终点对照全部完成且评分有效，才算完整实验结束。部署模型、下载数据、完成预跑都不是实验成功。本文件先记录协议，后续追加真实运行状态；未测得的成绩和耗时保留为空。

| 项目 | 固定值 |
|---|---|
| 实验目录 | `experiment/reproduction/wildclawbench-qwen3.8-27b/` |
| 研究方法 | 共享技能、成功和失败轨迹聚合、原生 Agent Evolver、真实任务 A/B、精确内容发布 |
| 用户 | 8 个固定身份 `user-01` 至 `user-08`，不是 8 个不同模型 |
| 白天／夜晚 | 6 / 6 |
| 每个白天 | 同一冻结共享库，完整 60 题，每个用户 7–8 题 |
| 主 Agent、进化器、普通和视觉评分 | GPU1 的 Qwen3.8-27B，本地 OpenAI 兼容接口 |
| 种子 | 42；服务和请求的实际参数同时记录 |
| 收费 API 预算 | **$0**；所有模型、语音和图片生成请求只走本地服务 |
| 固定任务执行 | 360 白天 + 120 终点对照 = **480**，预跑和夜间验证另计 |

## 2. 来源、固定版本和初始技能

| 资源 | 来源 | 固定 revision |
|---|---|---|
| WildClawBench 代码、原始任务、公开技能 | [官方仓库](https://github.com/InternLM/WildClawBench/tree/8e1fde746584ae1a9c49b70526d49a7a572625cd) | `8e1fde746584ae1a9c49b70526d49a7a572625cd` |
| Docker 镜像和工作区素材 | [官方数据集](https://huggingface.co/datasets/internlm/WildClawBench/tree/75f945578aa00cbdb8f46e4d42e4f4e98f704b4f) | `75f945578aa00cbdb8f46e4d42e4f4e98f704b4f` |
| 主模型权重 | [Qwen3.8-27B](https://huggingface.co/Qwen/Qwen3.8-27B) | `1d4bf0f2ff6012fd82039f2fa52739d0dd7c60c0` |
| 音频模型 | [Qwen2.5-Omni-7B](https://huggingface.co/Qwen/Qwen2.5-Omni-7B) | `ae9e1690543ffd5c0221dc27f79834d0294cba00` |
| 图片模型 | [SDXL base 1.0](https://huggingface.co/stabilityai/stable-diffusion-xl-base-1.0) | `462165984030d82259a11f4367a4eed129e94a7b`，FP16 Diffusers 权重 |
| SkillClaw 方法 | [论文 v2 §2–3](https://arxiv.org/html/2604.08377v2)及本仓库原生 Agent Evolver | Shared-Skill Git commit 在启动记录中保存 |

初始共享库 S0 是公开任务直接引用且实际存在的 **9 个完整技能 bundle**：`03_task1`、`03_task2`、`03_task3`、`03_task4`、`03_task5`、`03_task6`、`agent-browser`、`self-improving-agent-3.0.5`、`video-frames`。所有用户可使用整库，任务不被限制为只能使用自己 Markdown 中写出的技能。

这 9 个来自固定代码版本的构造规则，**不是声称作者原实验初始库恰好有 9 个**。不加入另外 18 个类别消融技能，不加入我们手写的良性验收技能。原仓库缺失的 `edge-tts` 不偷偷补成第十个技能；本地音频服务作为环境能力提供。来源审计详见 [paper-experiments.md](../../../docs/paper-experiments.md) 和 [来源 manifest](../../../reproduction/reference/wildclawbench-source-manifest.json)。

**9 个是可进化的共享库数量，不是 Agent 能看到的全部技能数。** 官方镜像另外自带 `scrapling-web-scraper`、`local-web-search-skill`、`web-search` 三个辅助目录；OpenClaw 还附带 53 个技能目录，是否出现在目录中取决于其依赖和 eligibility。首次真实容器目录列出共享库 9 项、镜像辅助 3 项、可用 bundled 7 项，共 19 项。镜像技能作为固定工具环境保留，不纳入发布库 S0–S6；其文件 hash 和实际目录证据保存在 `runtime/assets/image-skill-inventory.json`，各题实际读取记录用于判断是否使用了它们。

冻结协议包含镜像 ID、环境技能清单 hash 和名称；新建共享技能不得用同名覆盖固定环境技能。初始公开库中已存在的技能仍可正常修改。摘要与聚合只以可进化库的实际读取为技能引用；完整环境技能读取证据另行保留。

任务类别数量为：Productivity 10、Code 12、Social 6、Search 11、Creative 11、Safety 10。稳定排序后按索引模 8 分给固定身份；同一用户各轮运行同一组任务。`runtime/prepared/task-manifest.json` 保存题目、来源 hash、身份和变体标记。

## 3. 环境、接口和推理配置

GPU1 在准备开始时为 H200 NVL，约 140 GiB 显存；具体时间、空闲量、进程和实际版本保存在 `runtime/main-service/process.json` 及日志。GPU0 的现有服务不参与本实验。

| 接口角色 | 默认本地地址 | 用途 |
|---|---|---|
| 主 Qwen 服务 | `127.0.0.1:18181/v1` | 文字、视觉、工具调用、进化和比较 |
| 本地路由和请求证据 | `127.0.0.1:18182/v1` | 强制采样参数、模型别名本地映射、原始请求／响应和错误记录 |
| 音频补充 | `127.0.0.1:18183/v1` | 音频理解、转录、中文／英文语音生成 |
| 图片补充 | `127.0.0.1:18184/v1` | 实际生成模特服装等图片 |

DinD 内部使用外层 Docker bridge gateway 访问同一台机器的这些接口，不能把内层容器的 `localhost` 当成宿主。服务接口不记录真实密钥；私有覆盖配置放忽略的 `local.json`。基础 `local-experiment` 是无收费能力的本地标记。

主模型使用 BF16、TP=1、131072 上下文、最多 8 条并发序列；初始显存预算 0.6，为辅助服务留空间。Thinking 开启、reasoning effort `medium`。每个主模型请求固定 temperature=1.0、top_p=0.95、top_k=20、min_p=0、presence_penalty=0、repetition_penalty=1、seed=42，输出额度统一为 16384；实际工具调用仍保留原来的 roles、工具 schema 和返回消息。原 GPT 评分请求中的 512／1024 等额度也由网关覆盖，因为 thinking 会计入 Qwen 输出预算，过小额度可能只产生推理而没有评分答案。Agent、评分器、进化器、比较器和两个对照分支采用相同额度，原请求与覆盖后请求都留档。[采样配置依据](https://huggingface.co/Qwen/Qwen3.8-27B)

辅助 GPU 工作串行调度；并发限制和实际内存变化记录在服务日志。同机单卡共享会产生排队，不能按用户数量简单除算耗时。浏览器使用免费网络检索；Brave 收费搜索关闭。Gmail、Slack、日历任务只使用 benchmark 的本地模拟服务。

任务和进化容器清空镜像内的代理变量，固定收费模型域名解析到容器 loopback，并仅给本地占位凭据；免费搜索、代码和素材下载仍可联网。策略和真实 DNS／本地接口探针保存在 `runtime/assets/network-policy.json`。这不是给题目追加提示词。

社会 mock 的官方 20% 错误注入保持启用。私有容器以独立 `sitecustomize.py` 初始化 Python random seed=42，不改原始 workspace 文件；A/B 恢复到相同初始随机状态，后续随机数由实际请求顺序消耗，各分支的 429／500 和重试情况留档。bootstrap 内容 hash 保存在每次执行产物中。

官方 bundled `openai-image-gen` 和 `openai-whisper-api` 原脚本硬编码 OpenAI 域名，不能仅靠 `OPENAI_BASE_URL` 完成本地化。实验在每个干净容器里将这两个脚本的 endpoint 固定改为本地网关，保存原／改后 hash；原镜像 archive 和共享库不修改。图像生成走真实 SDXL，转录走真实 Omni。OpenClaw 原生 TTS 也固定到本地 `audio/speech`，关闭 Edge 与 ElevenLabs 回退；`speech.platform.bing.com` 同样被阻断。音色使用 Omni 的 Chelsie／Ethan，不能冒称与原题指定的 Edge 音色相同。这些是整个实验统一采用的工具环境差异。

原生 TTS 使用每题独立 prefs 文件、固定 provider／model／voice，关闭文本中的 provider 覆盖，单次 timeout=120 秒；完整任务 timeout 仍沿用各题原值。

评分器的本地模型 HTTP 等待时间统一为 `grading_timeout_seconds=1800` 秒，并受同为 1800 秒的整个评分进程上限约束。原评分代码中较短的 SDK 等待时间无法适应单卡八用户排队；只对当前任务的本地评分 URL 覆盖 HTTP timeout，保存原值和覆盖值。mock 请求、免费资源下载和 Agent 任务限时保留原值。这是本地评分后端适配的一部分，不改变评分权重或标准。

官方 OpenClaw 镜像 archive 为 13,479,787,008 bytes，固定 workspace 约 1.01 GiB，另外下载官方准备脚本要求的视频和 SAM3 权重。专用 DinD 容器及内层镜像、可写层位于 `runtime/docker/` 的 `/home` 磁盘，不改共享 daemon、全局 Docker context 或其它项目容器。

实际载入的 OpenClaw 是 `2026.3.11 (29dc654)`；官方 archive 的 SHA256 为 `22bd175c0e9a62a9e5c68cb7cb723c731403142b5946bff4421e8e1b495dab2e`。准备完成后，60 题输入共 2,979 个文件、15,734,352,296 bytes。2,917 个数据集文件均通过 HF 原对象的 SHA256／Git blob SHA1 校验；另外 62 个是官方准备流程补充的视频、SAM3 和解包文件，冻结本次下载的 SHA256，不能称为 HF 原对象校验或公开固定远程 revision。完整输入清单在 `runtime/prepared/input-files.json`；任务开始前检查文件、目录、权限和变更指纹，变更则停止，不跨输入版本复用成绩。

## 4. 与论文和原始 benchmark 的差异

| 项目 | 论文／原始 benchmark | 本实验 |
|---|---|---|
| 进化主模型 | SkillClaw 为 Qwen3-Max | Qwen3.8-27B |
| 任务分配 | 8 用户、6 白天／夜晚；未公开每一天具体题目列表 | 每天全部 60 题，固定 8 身份 |
| 初始库 | 原实验完整 manifest 未公开 | 当前公开构造规则得到的 9 个 |
| 评分 | WildClawBench 多题默认 GPT-5.4，配音题默认外部音频模型 | 本地 Qwen 文字／视觉、本地 Omni 音频；评分后端改变 |
| 生成工具 | 部分任务示例使用 OpenRouter、edge-tts | 本地语音和 SDXL；保留真实功能，记录工具差异 |
| OCR 02.06 | GPT-5-mini-2025-08-07，评分含其 810–835 分区间 | 用户批准改为本地 Qwen 目标；不能继续套用 GPT 的预期分数区间 |
| 终点评测 | 表 3 报告 Day 1–6，Day 6 使用 S5 | 另增加 S0/S6 各 60 题，测第六晚后的最终库 |
| 接受阈值 | 论文给出真实 A/B 和模型比较，未公开完整数值规则 | 第 6 节预先明确的量化规则 |

OCR 原文完整保存；变体同时说明本地目标、逐样本输出与重新核算汇总的要求。评分考察真实完成 OCRBench v1 评测和报告一致性，不奖励猜测原 GPT 分数。实际变体 Prompt／评分代码的 diff、hash 和运行产物单独保存并列在报告中。其余 59 题保留原始任务 Prompt；环境接口和评分后端的本地化仍然影响它们，不能称为完全无差异。

## 5. 六轮执行次序和证据范围

| 白天 | 任务库 | 夜间发布 |
|---|---|---|
| Day 1，基线 | S0，60 题 | Night 1 → S1 |
| Day 2 | S1，60 题 | Night 2 → S2 |
| Day 3 | S2，60 题 | Night 3 → S3 |
| Day 4 | S3，60 题 | Night 4 → S4 |
| Day 5 | S4，60 题 | Night 5 → S5 |
| Day 6 | S5，60 题 | Night 6 → S6 |
| 终点对照 | S0 和 S6，同题各 1 次 | 不再进化 |

每题使用全新容器、工作区初始副本和 mock 服务状态；跨题／跨轮只继承共享技能，不继承某个用户上次的文件、聊天或副作用。每个白天共享库不变，记录每题的库 hash、任务输入 hash、身份、seed、开始／结束时间、token 用量、工具错误、超时、实际读过的技能和产物。

夜间只使用刚结束白天的 60 条真实轨迹，同时保留成功和失败。原生 summarizer/aggregation 的技能引用和无技能分组形成改进／新建／跳过提案；每个提案在独立私有存储、受限候选工作区运行 Agent Evolver。预跑、验证和终点对照轨迹不进入进化。

## 6. 真实验证、接受和发布

1. 候选的来源 session IDs 必须能回溯到白天 task IDs。对所有来源相关任务分别运行旧库和候选库，不只挑一个有利样例。
2. 每个分支恢复同一份任务初始文件和服务状态，使用完整 OpenClaw 工具链。初始种子、推理参数、timeout 和评分协议相同，每分支 1 次。
3. 任一相关任务退分则拒绝。全部不退分时，平均质量提高，或者质量持平且工具错误／超时减少，并且本地 Qwen 对实际执行结果的比较支持候选，才接受；完全平局拒绝。任务超时是有效失败；评分接口失败是待评分，不是零分。
4. 比较按题隐藏旧／新标签，固定换序；给比较模型实际得分、评分诊断、稳定性和最终轨迹窗口（最多 48000 字符），完整轨迹仍留档。任何题比较认为候选更差则拒绝；只有平局也拒绝。这是本实验的明确化实现，不能冒称作者公开了相同提示词。
5. 同晚候选逐个验证；接受后的工作库成为后续候选的父库。不做额外的每晚全 60 题回归 A/B，相关任务验证不保证其它任务绝不退步。
6. 夜间结束原子发布经过验证的确切文件，包括 scripts/assets，保存父库、候选 bundle、候选整库和最终库 hash。发布后不调用 LLM merge 再改内容。没有接受项也发布同内容的下一轮快照。

新技能的 A 分支是未加入该技能的当前库，B 分支是加入完整候选的库。删除／改写非目标技能、来源不明、文件穿越、内容 hash 不符均停止该提案，不能默默部署。

先检查量化门槛；已经退分或完全持平的候选直接拒绝，标记 `not_run_numeric_gate_failed`，无需再花时间请求模型比较。只有通过门槛的候选才进入 Qwen 比较；比较失败保留待处理，不能默认为支持。

## 7. 预跑、恢复和验证

先测文字、工具调用、图片、音频理解／TTS、图片生成接口，再预跑 8 题：`01.03` BibTeX、`02.01` SAM3、`03.06` 跨部门更新、`04.03` 约束检索、`05.11` 配音、`06.01` 文件覆盖安全、`02.06` 本地 OCR 变体、`05.06` 服装图片。预跑的失败也记录；接口／输入缺失必须修复，不能靠预跑低分绕过环境问题。Agent 正常运行却未完成任务可以得到真实零分。

`frozen-protocol.json` 保存开始时所有参数和题目 hash。恢复键包含任务、阶段、轮次、分支、候选、技能库 hash、参数 hash、输入 hash 和 seed。完成结果复用；待评分保留 Agent 输出重评分；配置或候选字节变化不跨版本复用结果。单个基础设施故障最多立即重试 1 次，仍失败则保留状态供修复和 `resume`。并发控制锁阻止两个调度器重复跑同一实验。

自动检查覆盖：任务数量和身份分配、初始库字节、候选不退分／平局规则、评分缺失、真实库发布／恢复 hash、路由只本地、并发和独立环境。这些检查验证实验实现，不是实验成绩。

## 8. 操作入口和文件索引

从仓库根目录运行：

```bash
uv pip install --python .venv/bin/python -r experiment/reproduction/wildclawbench-qwen3.8-27b/requirements.txt
.venv/bin/python experiment/reproduction/wildclawbench-qwen3.8-27b/services/main_qwen.py start
./experiment/reproduction/wildclawbench-qwen3.8-27b/services/setup_auxiliary.sh
.venv/bin/python experiment/reproduction/wildclawbench-qwen3.8-27b/services/router_control.py start
.venv/bin/python experiment/reproduction/wildclawbench-qwen3.8-27b/services/auxiliary_control.py start audio
.venv/bin/python experiment/reproduction/wildclawbench-qwen3.8-27b/services/auxiliary_control.py start image
# 在另一个终端准备和执行；音频／图片服务启动命令见 services/ 内脚本。
.venv/bin/python experiment/reproduction/wildclawbench-qwen3.8-27b/run.py prepare
.venv/bin/python experiment/reproduction/wildclawbench-qwen3.8-27b/run.py pilot
.venv/bin/python experiment/reproduction/wildclawbench-qwen3.8-27b/run.py run
# 中断后：
.venv/bin/python experiment/reproduction/wildclawbench-qwen3.8-27b/run.py resume
.venv/bin/python experiment/reproduction/wildclawbench-qwen3.8-27b/run.py status
.venv/bin/python experiment/reproduction/wildclawbench-qwen3.8-27b/run.py report
```

`run` 在缺少同一实验预跑记录时先自动预跑；不会自动宣称任务/服务可用。需要新独立实验时传 `--run-dir <新目录>`；修改冻结参数不能沿用已运行记录。

| 路径 | 内容 |
|---|---|
| `config.json` | 公开预注册参数；私密覆盖 `local.json` 不提交 |
| `protocol.py / run.py` | 内容一致性、调度、实际任务 A/B、终点配对 |
| `environment.py / task_runtime.py` | 官方资源、专用 DinD、真实执行与评分 |
| `provenance.py / runtime/prepared/input-files.json` | 全部原始输入的文件 hash、来源校验与恢复前检查 |
| `evolution.py` | 原生 Agent Evolver 适配，私有候选和来源信息 |
| `gateway.py / services/` | 本地推理接口和完整请求证据 |
| `runtime/assets/` | 官方输入、下载 manifest、补充资源准备日志 |
| `runtime/api-evidence/` | 请求、响应、SSE、后端错误；不提交大体积／敏感日志 |
| `runtime/runs/<run>/banks/S0…S6/` | 完整不可变库快照 |
| `runtime/runs/<run>/executions/` | 每个白天、验证、终点的真实任务日志和产物 |
| `runtime/runs/<run>/nights/` | 提案、来源、逐项接受／拒绝、发布 hash |
| `runtime/runs/<run>/report/` | 逐题 CSV、终点配对 CSV、JSON、六领域 PNG/PDF、报告 |

## 9. 分析和耗时

报告六领域逐日曲线和总体题均分；Day 1 → Day 6 使用 S0 → S5，终点 S0 → S6 单列。终点按题配对，报告 60 题总体、59 道保持原提示的子集和 OCR 变体。接受/拒绝数量、技能新增/变化、工具稳定性和各阶段耗时同时报告。一种模型、一颗 seed、同一批任务用于学习和评测，不支持泛化到新任务或统计显著性的强结论；改善也可能为零或负数。

**准备前排期为整体 1–3 天，正式运行暂估 12–36 小时，尚未实测。** 官方同型号 60 题累计任务时间 516 分钟，作为参考，不等于本机墙钟时间。[官方时间记录](https://github.com/InternLM/WildClawBench/blob/8e1fde746584ae1a9c49b70526d49a7a572625cd/README.md)

- 固定部分：480 次执行；预跑另 8 次。
- 夜间验证：`2 × Σ候选相关任务数` 次；还包括摘要、提案和逐题比较。
- 首次模型编译、素材下载、依赖安装、环境初始化和评分另计。网络视频失败、SAM3 获取等属于准备障碍，不能记成 Agent 任务成绩。
- 预跑后保存真实墙钟、任务累计时间和固定部分外推；八用户共用一张 GPU，外推注明样本偏差和排队影响。

## 10. 实施日志

- 2026-10-12：用户批准本协议，开始实现。初次主服务遇到深目录 IPC 长度限制、随后遇到子进程 PATH 缺少 ninja；保留日志并在本实验启动配置中修复。没有改动 GPU0 的服务。
- 2026-10-12：真实素材、镜像和补充模型已准备；全部 60 题输入校验通过。本地六项接口实测通过：文字、函数调用、红色图片识别、TTS、音频转录、SDXL 实际图片生成；原始证据在 `runtime/preliminary-interface-checks/`，这些接口检查不计入任务成绩。
- 2026-10-12：真实容器 smoke 发现官方镜像的代理设置会阻断本地地址，已在实验容器中清空；随后发现工具 JSON schema 中的 `type` 对象触发网关异常，已修复并增加回归检查。两次均记录为基础设施故障，未虚报零分或完成任务。
- 2026-10-12：独立 smoke 已跑通实际 OpenClaw 任务执行、官方评分、有效 token 统计；原生进化器读取全部 9 个技能并用真实工具写出私有候选，原库字节未变。证据分别在 `runtime/smoke/` 和 `runtime/native-evolver-smoke/`，均不计入预跑、白天成绩或夜间学习证据。
- 2026-10-12：确定主模型统一 16384 输出额度，解决 thinking 耗尽原 GPT 小额度的问题；真实任务保留原输出后，仅重新评分，得到有效 Qwen JSON 和 `judge_method=llm`。明确评分解析失败为待评分，任务产物缺失仍为有效任务失败。正式启动前，64 项测试、136 项子测试及 Ruff 全部通过。
- 2026-10-12：首个预跑运行 `20261012T213129Z` 的真实社会任务成功用 `~/skills/03_task6/SKILL.md` 读取技能，暴露记录器漏识别 home 缩写的归因问题。终止本实验自己的预跑进程，保留旧容器轨迹，修复后以新运行编号和新代码 hash 重启；旧记录标记 `superseded_during_pilot`，没有产生正式 Day 1 成绩，不复用为新版本结果。
- 2026-10-12：真实旧轨迹已正确归因到 `03_task6`；两个独立容器 Python 进程验证 seed42 初始序列一致，20% 注错仍启用。重启前最新检查为 68 项测试、138 项子测试、Ruff 全部通过。
- 2026-10-12 21:38 UTC：代码 `f48d8a0` 冻结后启动独立运行 `20261012T213825Z`；协议 hash 为 `dfdc4a9791ccea6618533514ce28fd55b8af1f5b7cb2e04dd042c584c909d3b8`，S0 hash 为 `083b44f303788aad687a6fdc4e85bfc3f47d049844f20782ba4d9c3b95aa5b01`。启动时 Git 工作区干净，完整 commit、硬件／依赖版本和进程身份见该运行的启动记录。调度器独立于终端运行，日志为 `runtime/runs/20261012T213825Z/controller.log`；只有本运行通过预跑后才进入 Day 1。
- 2026-10-12：本运行六项接口检查全部通过。真实预跑 `05.06` 已产出 768×1152 PNG；`05.11` 已产出 24 kHz、31.06 秒 WAV，单次 TTS 墙钟 93.18 秒。`05.11` 的实际预跑分数为 0.8657、总墙钟约 488 秒；`04.03` 的实际预跑分数为 0，官方评分判定其答案不符合题目约束，属于有效任务失败。原始工具响应、评分请求和文件 hash 保留在运行目录和 `runtime/api-evidence/`。这些不是正式成绩，也不进入夜间学习证据。
- 2026-10-12 21:48 UTC：运行 `20261012T213825Z` 的 `06.01` 出现评分 SDK 超时：本地 Qwen 约 91.55 秒后实际返回有效 JSON，但原评分器已放弃等待。评分 guard 正确保留 `pending_score`，未采纳 `regex_fallback` 的备用分数。仅终止并归档本运行，统一本地评分 HTTP 等待时间后重新冻结；此运行也标记 `superseded_during_pilot`，未进入正式 Day 1，上述已完成预跑结果不复用到新运行。
- 2026-10-12：评分 timeout 适配仅匹配当前任务网关的 scheme、netloc 和 `/v1` 路径边界；httpx、requests、urllib 三种评分请求均保留修改前后值。加入原生 `EVOLVE_AGENTS.md` 的协议 hash，防止恢复时改变进化指令。重新启动前，75 项测试、155 项子测试和 Ruff 全部通过。旧运行的八题实际 chat 已归档，仅清理其剩余六个容器，主模型、音频、图片及路由服务继续运行。
- 预跑及正式成绩以随后真实运行记录为准。

<!-- LIVE_STATUS -->
## 当前运行记录

- 状态：`superseded_during_pilot`。
- 运行：`runtime/runs/20261012T213825Z/`。
- 更新：`2026-10-12T21:48:34.783590+00:00`。
- 已记录任务执行：3；终点有效配对：0/60。
- [运行报告](runtime/runs/20261012T213825Z/report/REPORT.md)。
