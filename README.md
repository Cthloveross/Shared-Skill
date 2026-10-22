# Shared-Skill

Shared-Skill 基于 SkillClaw，研究任务经验如何进入共享技能，以及不可信轨迹如何影响技能进化和后续任务。公共 Python 包为 `sharedskill`；每项实验有自己的入口、配置和证据。

## 实验索引

| 实验 | 状态 |
| --- | --- |
| [30题、三天三夜 clean／poisoned 对照](experiments/dymalskill-trajectory-e2e/README.md) | 已完成；分数、技能接受和文字停点见实验记录；S3未评完整30题Utility |
| [PoisonedEvolution SpreadsheetBench 单轮复现](experiments/poisoned_evolution/README.md) | Qwen3.5续接`-06`真实smoke 4/4通过，正式矩阵后台运行，已复用旧12次尝试并完成第13批；沿用原预算，425项离线测试通过；旧`-04`离线校准9/100 |

历史实验：[十二类合成轨迹](experiments/dymalskill-trajectory/README.md)、[早期合成机制](experiments/poisoned-evolution-mechanism/README.md)、[已终止的PoisonedEvolution纵向适配](experiments/poisoned-evolution-benign-wildclawbench-30x3/README.md)、[Qwen进化／DeepSeek验收](experiments/wildclawbench-30-deepseek-flash/README.md)、[机制对齐诊断](experiments/wildclawbench-30-deepseek-flash-alignment/README.md)、[全本地60题](experiments/wildclawbench-qwen3.8-27b/README.md)。各实验独立计分，完成、失败和未覆盖范围以对应README为准。

## 目录分工

| 目录 | 用途 |
| --- | --- |
| `sharedskill/`、`evolve_server/` | 正式客户端、共享技能与进化服务代码 |
| `scripts/`、`tests/` | 现有通用驱动和测试，包括此前三天三夜管线 |
| `experiments/` | 每项实验的入口；独立复现可在自己的目录内包含专用代码和测试 |
| `docs/` | 总研究问题、方法、架构与论文来源 |
| `injection/` | 轨迹注入配置 |
| `reproduction/reference/` | 已有第三方数据、评分器和初始技能参考材料，供历史配置定位 |
| `benign_validation/` | 公共客户端的安全工作流验收工具 |
| `artifacts/<ID>/` | 冻结配置、源码、技能快照和证据 |
| `outputs/<ID>/` | 原始日志、指标与报告 |
| `runtime/` | 隔离引擎、虚拟环境及运行依赖；不是第二份实验定义 |

**新实验只使用 `experiments/`。** 单数 `experiment/` 是迁移后保留的旧运行残留，包含权限受限的Docker数据目录，没有新的实验入口；[历史保留原因与归档映射](artifacts/30x5-sixcycles-20261016-01/archive.json)记录的是迁移时状态，当前模型服务以所属实验README为准。

Git保存源码、测试、配置和说明。`artifacts/`、`outputs/`、`runtime/`及第三方数据不随push上传；指向这些目录的证据链接需要在保有对应本地产物的工作区打开。克隆仓库不等于取得完整运行环境或原始实验数据。

根目录的两个 `*.egg-info` 已清理。它们是安装工具自动生成的包元数据，不是源码；后续执行构建或安装时可能重新出现。`.venv/` 是仓库自身的Python环境，与 `runtime/` 内各引擎的隔离环境用途不同。

`runtime/wildclawbench/` 保留此前管线依赖、模型与历史请求证据；`runtime/poisoned-evolution/` 仅供单轮复现的原生SkillClaw引擎使用。模型权重、任务工作簿和Docker数据不按缓存删除。

## 使用入口

从仓库根目录运行。PoisonedEvolution的命令和专属测试见[该实验README](experiments/poisoned_evolution/README.md)。旧实验配置仅用于只读检查，不恢复已完成的final。

```bash
.venv/bin/python -m scripts.injection.e2e_cycles --config experiments/dymalskill-trajectory-e2e/config.json --run-id 30x5-sixcycles-20261016-01 --dry-run
.venv/bin/python -m experiments.poisoned_evolution status
.venv/bin/python -m scripts.archive resolve --run-id ID --path ORIGINAL_ABSOLUTE_PATH
```

旧路径通过归档映射读取，不改历史JSON或digest。原根目录的DyMalSkill下载包和早期进化日志已分别归入[历史输入](artifacts/30x5-sixcycles-20261016-01/legacy/repository/DyMalSkill_300x12.zip)与[原始日志](outputs/30x5-sixcycles-20261016-01/legacy/repository/evolve_history.jsonl)，原字节保留。

## 安装与文档

需要Python 3.10+、uv和可用模型后端：

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e '.[server,validation]'
source .venv/bin/activate
sharedskill setup
sharedskill start --port 30000
```

客户端配置默认为 `~/.sharedskill/config.yaml`，可用 `SHAREDSKILL_CONFIG_FILE` 指定。操作入口见[文档索引](docs/README.md)；研究设计见[INSTRUCTION](docs/INSTRUCTION.md)，架构见[architecture](docs/architecture.md)。

基于 [AMAP-ML/SkillClaw](https://github.com/AMAP-ML/SkillClaw)，保留[MIT许可](LICENSE)；第三方边界见[upstream](docs/upstream.md)。本地实验不代表完成原论文全部数值复现。
