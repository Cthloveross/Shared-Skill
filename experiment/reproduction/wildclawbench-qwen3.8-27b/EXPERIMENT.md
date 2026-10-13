# Qwen3.8-27B 全本地六轮技能进化实验

## 1. 实验身份和完成标准

这是 **SkillClaw 方法的本地模型复现**，研究问题是：同一批 60 个真实工具任务，经 8 个模拟用户的运行证据和六晚共享技能更新后，任务成绩改善多少？不能直接把本实验数字称为原论文 Qwen3-Max 表 3 的复现数字。

只有六个白天、六个夜晚、最终 S0/S6 终点对照全部完成且评分有效，才算完整实验结束。部署模型、下载数据、完成预跑都不是实验成功。本文件先记录协议，后续追加真实运行状态；未测得的成绩和耗时保留为空。

### 1.1 主指标与完成率的区别

**评价重点是 task 的完成效果。主指标是任务完成质量均分：`100 × Σ overall_score / 60`，单位为分（0–100）。** 每题 `overall_score` 是固定评分协议得到的 0–1 分数，沿用原题规则及已批准的 OCR 本地变体。真实失败可以得 0，超时按已有产物评分；基础设施／评分故障保留缺失，不补零，也不从完整实验的分母里偷偷删除。[官方汇总代码](https://github.com/InternLM/WildClawBench/blob/8e1fde746584ae1a9c49b70526d49a7a572625cd/src/utils/grading.py#L309)

| 名称 | 定义 | 用途 |
| --- | --- | --- |
| 完成质量均分 | 全 60 题评分均值 ×100；未评完只给带 `n/60` 的部分结果 | 主要比较 S0 与 S6、六个白天的任务表现 |
| 有效评分覆盖率 | 有效评分题数／应评题数；预跑分母 8，白天和单个终点分支分母 60 | 判断实验进度和成绩是否完整 |
| 满分任务占比 | `score=1` 的题数／应评题数；未全评分不报告最终比例 | 辅助展示得到满分的题数 |
| 整题成功率 | 需要独立明确的逐题成功条件；当前无统一官方二元阈值，保持未定义 | 不把均分或 `completed` 状态冒充成功率 |
| 技能使用／候选决策 | 实际读取的共享技能、涉及任务、修改／新建／跳过、接受／拒绝、确切版本 | 解释主成绩变化，不以“改了多少技能”作为主成绩 |

例如，当前 Day 1 的 `60/60 completed` 表示全部执行和评分结束；其完成质量均分为 **49.16/100**，20 题零分、11 题满分（满分占比 **18.33%**），19 题超时。这不是“任务成功率 100%”，也不能把 49.16 写成“49.16% 的整题成功”。最终进步以相同配置的 S0／S6 终点配对为准；同库重复测量波动不能自动归因于技能更新。

### 1.2 一轮按什么顺序执行

| 阶段 | 输入与执行 | 输出／进入下一阶段的条件 |
| --- | --- | --- |
| Day k | 冻结当日 S(k−1)；8 个固定用户分配全部 60 题，每题每轮由一个固定用户执行，使用干净环境、按需选技能 | 保存真实轨迹、产物、得分与技能读取；60 题全部有效评分后进入 Night k |
| Night k 提案 | 只分析当天成功与失败证据；按实际引用的技能归组，无技能会话用于发现新技能 | 私有候选；证据不足可以跳过，不强迫所有技能修改 |
| Night k 验证 | 对每个候选的所有来源相关任务执行旧库／候选库 A/B，使用同样输入和完整工具链 | 按第 6 节冻结的数值门槛与 Qwen 比较接受或拒绝；评分故障暂停等待恢复 |
| Night k 发布 | 顺序应用接受项，拒绝项留在候选区 | 原子发布确切经过验证的 S(k)，全部夜间步骤结束后才供下一天使用 |
| 终点对照 | 六晚结束后，S0／S6 各独立跑全部 60 题，不再进化 | 报告总体、六类别、逐题差值及 OCR 变体；过程日志用于核对归因 |

Day／Night 是连续执行的实验阶段，不等待现实昼夜。六轮提供六次更新机会，不保证九个初始技能都被使用或改进；新增技能在后续轮次可被选择，也不保证一定被读取。本次规范仅澄清指标和报告，不修改运行中的评分器、模型、任务、接受规则或冻结源码。

当前控制器已载入旧报告模块，由本次运行专用的导出进程刷新报告口径，随控制器退出；不调用模型或重跑任务。进程命令与身份记录于 `runtime/runs/20261012T230206Z/metrics-report-process.json`。

### 1.3 无人值守续跑：用户新增授权

2026-10-13 用户要求睡眠期间错误可以跳过、流水线继续推进。该授权调整**控制器的错误处理**，保留原任务、模型、评分、接受门槛、60 题计划和冻结源码；独立控制器策略及其源码 hash、授权登记时间、重试和跳过记录另行保存，不把这些变化藏进原冻结协议。

| 情况 | 续跑动作 | 成绩与发布约束 |
| --- | --- | --- |
| 当前控制器正常运行 | 监护等待，不启动第二个控制器 | 复用原运行与已完成结果 |
| 单题自动重试后仍有执行／评分错误 | 持久记录跳过项，继续同用户的后续题 | 原错误与 `pending_score` 证据保留，分数缺失，不补零或改成功；夜间只使用有效评分的当日轨迹 |
| 候选相关 A/B 或比较不能完成 | 拒绝该候选，继续下一项 | 不发布未通过完整验证的候选；记录失败任务和确切版本 hash |
| 当晚提案不能完成 | 原子发布与父库相同的快照，进入下一天 | 明确该晚没有更新及其协议偏离 |
| 控制器退出／真正停滞 | 核验进程身份后接管，复用持久记录 | 长任务仍产生结果／模型成功请求时不因观察超时重启 |

无人值守策略允许不足 60 题有效评分时进入后续阶段，因此与第 1.2 节原严格闸门有所不同。所有计划分母仍为 60，均分缺题时标为部分结果；任何任务或夜间步骤被跳过，结束状态为 `finished_with_gaps`。**流水线走完不等于原完整实验完成**，只有有效评分及方法证据满足原完整要求，才可报告完整结果。

监护入口为 [operations/unattended.py](operations/unattended.py)，仅在连续 7200 秒既无任务／状态事件进展、也无本地模型成功请求时接管真正停滞的控制器。终止范围限于经 PID、UID、启动时间核验的专用进程组；容器清理限于本运行精确标签对应的未完成任务。长任务和正常模型推理不会因一次观察超时被重启。

2026-10-13 05:43 UTC 已独立启动监护 PID `3840089`，当前等待原控制器 `3622775`，不重复启动任务。登记时间、命令、资源预算和精确身份见 [supervisor-process.json](runtime/runs/20261012T230206Z/operations/unattended/supervisor-process.json)；策略源码快照、日志与持久跳过项均位于同目录。启动方式：

```bash
.venv/bin/python experiment/reproduction/wildclawbench-qwen3.8-27b/operations/unattended.py supervisor \
  --run-dir /home/tc442/projects/Shared-Skill/experiment/reproduction/wildclawbench-qwen3.8-27b/runtime/runs/20261012T230206Z \
  --authorized-at 2026-10-13T05:43:04.676352+00:00 --stale-seconds 7200
```

监督锁防止重复监护，原控制器锁防止重复执行；关闭聊天不影响已脱离终端的实验和监护进程。新增故障链与停滞恢复检查后，115 项测试、175 项子测试通过，70 份原冻结源码 hash 全部未变。

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

| 辅助模型 | 固定实现的默认参数 |
|---|---|
| Omni | BF16、seed=42；thinker 贪心解码，默认输出额度 2048；talker 输出额度 4096，采样开启，top_k=40、top_p=0.8、temperature=0.9、repetition_penalty=1.05；原始音频 24 kHz |
| SDXL | FP16、30 steps、guidance_scale=7.5、默认 1024×1024；多图逐张生成，seed=42+图像索引 |

工具可以按任务传入图片尺寸等参数，实际请求和有效输出参数另行留档。上述服务、默认值和适配代码在全部阶段保持一致；辅助模型不参与技能提案和候选比较。

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

OCR 原文完整保存；变体同时说明本地目标、逐样本输出与重新核算汇总的要求。评分从冻结的宿主原始 TSV 取 question／image／groundtruth，逐字节检查导出 TSV 一致；1000 个唯一样本的 response_id、预测和实际请求均对应本运行真实网关证据。真实 `content=null / finish_reason=length` 以空答案计该样本 0 分，并保存输出额度失败及 token 诊断；其他响应改写或缺失不能当成功。模型 OCR 准确率与任务完成分分别报告。评分考察真实完成 OCRBench v1 评测和报告一致性，不奖励猜测原 GPT 分数。实际变体 Prompt／评分代码的 diff、hash 和运行产物单独保存并列在报告中。其余 59 题保留原始任务 Prompt；环境接口和评分后端的本地化仍然影响它们，不能称为完全无差异。

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

任务自身达到限时，保存部分轨迹及产物并正常评分，`timed_out=true`。原生 OpenClaw 会在截止时取消模型流；分类必须结合明确的 `embedded run timeout` 日志中的 session ID／限时或外层执行截止证据。单独的 `meta.aborted` 也可能来自手动中断，不能据此判为任务超时。真实 HTTP500、网络错误及截止前的模型故障继续保留为基础设施错误。

自动检查覆盖：任务数量和身份分配、初始库字节、候选不退分／平局规则、评分缺失、真实库发布／恢复 hash、路由只本地、并发和独立环境。这些检查验证实验实现，不是实验成绩。

稳定性字段 `tool_errors` 统计原生 `toolResult.isError` 的工具调用失败；子脚本内部捕获并恢复的 HTTP 错误可能不使整次工具调用失败，仍逐条保存在请求及工具原始日志中。网关错误、服务超时和任务超时另有记录，不能把 `tool_errors=0` 解释为所有内部请求都成功。

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
| `tests/` | 协议、真实官方题目兼容性、运行器、进化器、网关和报告回归检查 |
| `runtime/assets/` | 官方输入、下载 manifest、补充资源准备日志 |
| `runtime/api-evidence/` | 请求、响应、SSE、后端错误；不提交大体积／敏感日志 |
| `runtime/runs/<run>/banks/S0…S6/` | 完整不可变库快照 |
| `runtime/runs/<run>/executions/` | 每个白天、验证、终点的真实任务日志和产物 |
| `runtime/runs/<run>/nights/` | 提案、来源、逐项接受／拒绝、发布 hash |
| `runtime/runs/<run>/report/` | 逐题 CSV、终点配对 CSV、JSON、六领域 PNG/PDF、报告 |

代码检查从仓库根目录运行：

```bash
.venv/bin/python -m pytest -q experiment/reproduction/wildclawbench-qwen3.8-27b/tests
```

## 9. 分析和耗时

报告六领域逐日曲线和总体题均分；Day 1 → Day 6 使用 S0 → S5，终点 S0 → S6 单列。终点按题配对，报告 60 题总体、59 道保持原提示的子集和 OCR 变体。接受/拒绝数量、技能新增/变化、工具稳定性和各阶段耗时同时报告。一种模型、一颗 seed、同一批任务用于学习和评测，不支持泛化到新任务或统计显著性的强结论；改善也可能为零或负数。

**准备前排期为整体 1–3 天，正式运行暂估 12–36 小时，尚未实测。** 官方同型号 60 题累计任务时间 516 分钟，作为参考，不等于本机墙钟时间。[官方时间记录](https://github.com/InternLM/WildClawBench/blob/8e1fde746584ae1a9c49b70526d49a7a572625cd/README.md)

- 固定部分：480 次执行；预跑另 8 次。
- 夜间验证：`2 × Σ候选相关任务数` 次；还包括摘要、提案和逐题比较。
- 首次模型编译、素材下载、依赖安装、环境初始化和评分另计。网络视频失败、SAM3 获取等属于准备障碍，不能记成 Agent 任务成绩。
- 预跑后保存真实墙钟、任务累计时间和固定部分外推；八用户共用一张 GPU，外推注明样本偏差和排队影响。

2026-10-12 当前冻结运行 `20261012T230206Z` 的完整 8 题预跑实测墙钟 **1216.02 秒，即 20.27 分钟**，任务累计 4637.93 秒；8/8 均得到有效评分，没有待评分或基础设施错误。按相同选题／并发条件用 `480 / 8 × 实际墙钟` 外推，固定部分约 **20.27 小时**，不含夜间摘要、提案、比较和 A/B。八题是特选的困难样本，并非随机代表全 60 题；此值只用于排期，不能当完整实验 ETA。整体暂仍按 **1–3 天**，待 Day 1 和第一个真实夜晚再次校准。原始测量见 [pilot-gate.json](runtime/runs/20261012T230206Z/pilot-gate.json)。此前被替换的预跑测得 20.36 分钟，保留在旧运行中，不复用其成绩。

### 9.1 实际耗时来源

2026-10-12 23:16 UTC 的独立测量见 [performance.json](runtime/runs/20261012T230206Z/audits/performance.json)。当时新预跑完成 6/8 题，没有待评分或基础设施错误。六项接口检查合计约 7.7 秒；三个模型服务持续驻留，任务间没有重新下载或加载权重。

- **实际推理和工具执行**：新 `06.01` 的一次 PDF 子请求耗时 244.6 秒、输出 9558 token，其中 reasoning 3048；该题 Agent 总计 343.1 秒，主模型请求覆盖约 339.4 秒，评分另用 53.4 秒。新服装任务正常完成约 502 秒，旧版同题曾达到 1200 秒限时。单张卡上的八用户并发不能把这些时间直接除以八。
- **OCR 批量请求与排队**：旧完整预跑的 OCR 产生 1113 次主模型请求，监控中等待队列最高 63。新预跑截至 23:16 的 87 个监控样本有 28 个出现排队，最高 10；前段无排队，后段 OCR 子调用开始后出现等待。排队和单次长推理都真实存在。
- **实验量**：六个白天加两个终点对照共 480 次任务执行。夜间还有 60 条轨迹摘要、按技能分组的原生提案、每个候选所有相关任务的两臂执行和比较。夜间数量尚未测得，不能由预跑直接预测。
- **此前调试重启**：五次被替换运行约占 65.3 分钟控制器活动时间，运行间约 25.3 分钟用于修复、检查、归档和启动。这约 90 分钟单列为实现调试；部分停止时间由状态记录近似，不能当精确任务耗时。旧运行的分数均不进入当前冻结实验。

下一次 ETA 使用 **Day 1 全 60 题的实际墙钟 × 8 个固定阶段**，再加首个夜晚测得的提案与验证工作量，并注明技能变化、候选数及任务时长变化。当前 12–36 小时／整体 1–3 天仍是排期范围，没有精确完成时刻。

### 9.2 交叉检查、修正与边界

独立审阅代码、冻结清单、原始题目、真实容器产物和后端请求，检查如下：

| 检查 | 当前证据／结论 |
|---|---|
| 输入与共享库 | 60 题原限时；59 题提示原样；只有批准的 OCR 本地变体。S0 是 9 个完整公开技能 bundle，不是 60 个技能 |
| 执行与接口 | 每题独立容器和初始状态；真实技能读取可以回溯到相同 bank bytes；已观测模型请求指向本地服务 |
| 冻结与恢复 | 正式目录 70 个被冻结源码 hash 未变；完成缓存包含阶段、轮次、分支、候选、输入、参数、bank 和 seed |
| 夜间证据 | 只从刚完成白天收集成功／失败；预跑、验证和终点不进入学习 |
| 候选与发布 | 代码和回归检查确认全来源相关任务 A/B、不退分门槛及 Qwen 比较；发布复制确切已验证字节，拒绝项不部署 |
| 实际进化收益 | 尚未完成第一晚，不能用静态检查或 smoke 证明六轮进化有效；最终以真实夜间记录和终点配对为准 |

本轮 OCR 的重复索引已核对到 Agent 自己生成的 `local_qwen3827b.py`：它用图像 MD5 反查单个样本 index，而官方 1000 个样本只有 930 张唯一图像，同一图像有不同问题。产物的 1000 个唯一响应被标为 930 个唯一 index，评分器拒绝重复索引是正确的；图像与 question 的组合才是 1000 个唯一样本。没有手动修正 Agent 产物或重新计分，本轮不能报告完整 OCR accuracy。完整交叉证据见 [flow-cross-check.json](runtime/runs/20261012T230206Z/audits/flow-cross-check.json)。

修正了报告读取夜间摘要路径错误（应为 `nights/NN/proposals/evolution/proposal-summary.json`），并限制结果及候选决策搜索为控制器拥有的固定目录层级，避免遍历 Agent 工作区中的同名文件。

原生 OpenClaw 的 usage 会把后端已经包含在 completion 中的 reasoning 再相加。例如新 `02.01` 的原生 output 为 13986，而 Agent 后端 output 为 9353，其中 reasoning 为 4633。报告现改读实际后端 JSON／SSE usage，不回写原始结果。任务 token 包含 Agent、同任务 OCR/PDF 子调用及评分，单列没有最终 usage 的请求数；不包含夜间模型请求或音频／图片辅助模型。缺失计量不能解释为零消耗。

代码整理在独立分支 `refactor/local-reproduction`：删除两个无人调用的重复验证脚本，复用协议 hash／JSON 函数，删除未消费的配置及状态字段；保留首次部署安装脚本和正式六能力接口检查。测试移入 `tests/`。报告和测试整理不属于冻结的实验行为，已应用于主分支；进化器与服务目录的清理留在分支，避免正在运行的任务和夜间进化读到中途修改的源码。当前控制器已加载旧报告模块，已安排其退出后自动用新模块导出一次报告，操作记录见 [report-refresh-process.json](runtime/runs/20261012T230206Z/report-refresh-process.json)；原始任务无需重跑，也可用上述 `report` 命令再次导出。

已知协议边界：技能身份 hash 覆盖路径和内容，单独改变执行权限不会作为新内容候选；候选验证覆盖来源相关任务，不构成全库 60 题不退步的保证；夜间提案中途失败恢复可能重新摘要和提案，增加成本。这些边界不应藏在“全部检查通过”的描述中，也不在本轮冻结后改变。

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
- 2026-10-12 21:52 UTC：代码 `9c35e9e` 已提交并推送，以干净工作区启动运行 `20261012T215250Z`；协议 hash 为 `537c5eed4c2063216718e09d6d492d93e52e57a73cf8560bd0cd0badaf511312`，初始库仍为上述 S0。六项接口检查全部通过，8 题预跑于 21:53 UTC 同时启动。实际启动命令和进程身份在该运行 `controller-process.json`，事件在 `controller.log` 和 `events.jsonl`。通过后自动继续六个白天／夜晚及两个终点分支，无需再次手动启动。
- 2026-10-12：上述运行的 `06.01`、`04.03`、`03.06` 分别完成并得 0.5、0、0.871；真实判分 timeout 从原 40 秒等值统一为 1800 秒，`03_task6` 的实际 home 路径读取正确归因到共享库，均无 pending／infra。但正式前源码审计确认原生任务截止取消流可能被误分类为 infra。为先修复计量，22:02 UTC 仅终止该预跑并标记 `superseded_during_pilot`；归档八题已有证据，仅清理其剩余五个容器，没有进入 Day 1。这三道分数也不跨代码版本复用。
- 2026-10-12 22:07 UTC：任务截止分类修复后，83 项测试、161 项子测试通过。独立真实容器检查沿用 `06.04` 原提示、仅在此检查中将限时缩为 2 秒，确实触发原生截止及 HTTP200 流取消；成功保存轨迹／产物并进入官方评分，结果 `completed / score=0 / timed_out=true`，没有误记 infra。全流程约 6.04 秒。证据在 [独立超时检查](runtime/task-timeout-probe/20261012T220700Z/probe-design.json)、[截止证据](runtime/task-timeout-probe/20261012T220700Z/agent-deadline.json)和同目录原生日志、评分输出。此检查不属于正式实验，正常 60 题仍使用原题限时。
- 2026-10-12 22:08 UTC：修复代码 `6212d1a` 已推送，以干净工作区启动新的独立运行 `20261012T220826Z`，协议 hash 为 `1d13fb688f920bd7883a498c8edccfe9d2dec7cd0e193ba591291574093896f4`。该运行从 S0 和全新 8 题预跑开始，通过后自动执行 Day 1–6、Night 1–6 及 S0/S6 终点对照。调度器身份和启动命令见其 `controller-process.json`；旧三个预跑均保留为被替换的预跑记录，没有正式成绩。
- 2026-10-12：上述运行已有七题完成，其中 BibTeX 和服装图像题达到原限时后正常保存及评分。最后的 OCR 任务确实发起了数百次本地模型评测请求；截止时的 Agent SSE 取消比工具的新并发评测请求更早，因此“仅最后一个模型请求可被认定为截止取消”的实现假设不成立，误记 infra。22:31 UTC 在 Day 1 前仅终止本运行并归档，改以明确的任务截止时间窗口判断本运行的 HTTP200 客户端取消；真实服务错误、提前取消和退出窗口外取消继续保留为 infra。此运行同样标记 `superseded_during_pilot`，七题分数不跨版本复用。
- 2026-10-12 22:34 UTC：并发截止分类已修复，最新 86 项测试、161 项子测试及 Ruff 全部通过。旧 OCR 的实际 HTTP200 取消记录与原始请求日志逐项一致，离线回放将其认定为截止取消，并确认进入产物保存／评分路径；回放中的 Docker 与评分为测试替身，不生成真实分数，也没有改写旧结果。支持多个活动流在截止窗口同时取消，提前断连、退出窗口外断连、HTTP500、ReadError 和模型显式 error 继续记为基础设施异常。后续重新从 S0 全新预跑，旧结果不复用。
- 2026-10-12 22:35 UTC：以已推送代码 `bd2c839` 和干净工作区启动独立运行 `20261012T223532Z`，协议 hash 为 `2a35a2911eceec396de61a4a4eb81726cfbfde9e0e4f0e3b48810c3cf47ff900`，S0 hash 保持不变。六项本地接口检查全部通过，8 题于 22:35:43 UTC 开始。控制进程独立于终端，PID 及身份、完整启动命令保存在 [controller-process.json](runtime/runs/20261012T223532Z/controller-process.json)，代码及环境版本保存在 [startup-inventory.json](runtime/runs/20261012T223532Z/startup-inventory.json)。预跑通过后同一调度器自动继续 Day 1–6、Night 1–6 和终点对照；此前四次运行均未进入正式阶段。
- 2026-10-12 22:56 UTC：运行 `20261012T223532Z` 的全部 8 题预跑完成有效评分，真实 OCR、服装和社会任务截止均保存产物并进入评分。收尾审计发现 OCR 评分应以冻结原 TSV 而非可写导出表为真值；本轮两表 SHA 实际相同，但自动约束不够完整。调度器于 22:56:05 UTC 自动启动 Day 1 八题，22:56:25 UTC 已核验 UID／PID／startticks／命令后停止；无正式任务完成。状态改为 `superseded_after_day1_started`，保留全部证据、归档活动容器，本轮任何预跑或启动任务成绩都不进入新冻结实验。另发现样本 670 的真实 Qwen 返回 `content=null / finish_reason=length`，输出额度已耗尽；Agent 保存空预测。新版按真实无答案样本计零，区分输出额度失败与伪造／不一致，不回写本次旧分数。
- 2026-10-12 23:00 UTC：OCR 评分的冻结原表锚定、导出表完整性检查及真实 null-length 空答案计分已修复；最终 93 项测试、171 项子测试和 Ruff 通过。加入 question／image／groundtruth／索引篡改、实际请求不匹配、原始宿主输入变化必须停止、空答案真实失败及伪造响应的回归。模型采样、16384 输出额度和 1200 秒题目限时保持一致。上一轮媒体真实性详见 [pilot-media.json](runtime/runs/20261012T223532Z/audits/pilot-media.json)，审计没有修改旧成绩。
- 2026-10-12 23:02 UTC：已推送代码 `9a28d1d`，以干净工作区启动独立运行 `20261012T230206Z`，协议 hash `91f8f1d9d70e2c74e645519f8667e5b598cf63a2c1a363b44ebc42de6b42529c`，初始9技能 S0 hash不变。六个本地接口检查全部通过；主控制进程 PID3283683 独立于终端，启动身份见 [controller-process.json](runtime/runs/20261012T230206Z/controller-process.json)，版本见 [startup-inventory.json](runtime/runs/20261012T230206Z/startup-inventory.json)。重新从完整预跑开始，通过后自动运行六个白天／夜晚和 S0/S6 终点对照。旧运行的 16 题证据（8 预跑、8 已启动但无完成成绩的 Day1）已归档，仅精确清理其8个活动容器。旧 OCR 真实1000条调用核验与独立准确率842/1000见 [pilot-ocr.json](runtime/runs/20261012T223532Z/audits/pilot-ocr.json)，原始任务分0.2保持不动，旧分数不跨版本复用。
- 2026-10-12 23:22 UTC：当前冻结运行 8/8 预跑通过接口／执行／评分门槛，同一调度器已自动进入 Day 1，首批八题实际启动。BibTeX 按原 900 秒限时正常评分 0；OCR 按原 1200 秒限时正常评分 0.2，失败原因为 Agent 产物重复样本索引，`verified_samples=0`，不能宣称本轮完成 1000 样本认证。评分锚定原 TSV，宿主及导出表 SHA 均为 `39458e4d42b1107f6646b8e0445769958466a1cb6301528ddb31ad3c5530aa4d`。本轮没有实际走到 null-length 样本认证分支，其处理仍由真实旧证据和回归检查支撑。结构清理分支 96 项测试、171 项子测试通过；主分支报告与测试整理 95 项测试、171 项子测试通过。70 份冻结源码未变，未因代码整理重启实验。
- 2026-10-13 01:01 UTC：Day 1 初次调度结束，59 题有效评分、1 题 `06.10` 被记为 infra；未进入 Night 1。59 题平均墙钟 9.56 分钟，18 题达到限时，整体首次调度约 99 分钟。这不是完整 60 题成绩。两份独立审阅确认 `06.10` 的 HTTP400 来自 Agent 生成的 `generate_report.py`，它发送不可识别的图像数据后采用 heuristic，并继续修改和打包；主 Agent 没有模型错误，最后正常达到 600 秒题目限时。运行器把工具内部输入错误归为主推理 infra，提前退出评分，是分类遗漏；未将其补零分或改 completed。
- 2026-10-13 02:43 UTC：70 份冻结源码逐 SHA 未变，在同一 run 内启动 `resume`，只重新执行未完成的 `06.10`，保留其余59份结果。恢复前 hash、HTTP400 原始证据及进程身份见 [recovery-20261013T024326Z.json](runtime/runs/20261012T230206Z/audits/recovery-20261013T024326Z.json)，旧控制器记录见同 run 的 `operations/20261013T024326Z/`。分类修正仅在独立分支准备，未悄改本轮冻结协议。原生 `status` 的递归搜索会把新增 attempt-history 中的旧 result 也计入；当前任务数量以 canonical 五层执行目录及修正后的报告为准，不把归档错误算成新任务。
- 2026-10-13 05:19 UTC：Night 1 最后候选 `visual-puzzle-solving` 的旧库分支 `02.05` 触发协议内自动重试。原始 HTTP400 来自 Agent 生成的过窄图片，修图后的请求已成功，最终达到原题 1200 秒限时；旧分类器仍因早先 400 将其记为 infra。没有修改冻结代码或填补成绩，原始请求、修复轨迹和重试证据见 [retry-02.05-20261013T051955Z.json](runtime/runs/20261012T230206Z/audits/retry-02.05-20261013T051955Z.json)。
- 2026-10-13：Night 1 前六项候选的独立版本链核验通过：接受 2、拒绝 4；12 份真实 A/B 结果和 2 份比较文件有效，精确重放后的 hash 与当前工作库一致，拒绝项未部署。此时 S1 尚未发布，不将中途工作库称为下一轮技能库，证据见 [night1-chain-inflight.json](runtime/runs/20261012T230206Z/audits/night1-chain-inflight.json)。
- 预跑及正式成绩以随后真实运行记录为准。

<!-- LIVE_STATUS -->
## 当前运行记录

- 状态：`night`。
- 运行：`runtime/runs/20261012T230206Z/`。
- 报告更新：`2026-10-13T04:54:24.240094+00:00`；阶段更新：`2026-10-13T02:54:06.991277+00:00`。
- 已记录任务执行：80；终点有效配对：0/60。
- [运行报告](runtime/runs/20261012T230206Z/report/REPORT.md)。
