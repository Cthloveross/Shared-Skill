# PoisonedEvolution：独立单轮复现实验

这里集中本实验的代码、测试、配置和说明。它使用SpreadsheetBench轨迹，每个trial只进化一次，与[此前30题三天三夜对照](../dymalskill-trajectory-e2e/README.md)是两项独立实验。

## 记录与状态

| 版本 | 记录 | 状态 |
| --- | --- | --- |
| 当前Qwen3.5-35B模型对照 `-05` | [canary/README](canary/README.md)、[config-05.json](canary/config-05.json) | 10月22日14:25 EDT真实smoke通过，100次正式对照已在后台运行 |
| 已完成canary F1–F4 `-04` | [config-04.json](canary/config-04.json) | 完成100/100；冻结自动SER 8/100（8%）、unknown 0；另有1例已确认语义漏判 |
| 已停止canary `-03` | [config-03.json](canary/config-03.json) | 方法修正前保留15个完整trial；原自动1/15，离线校准3/15；第16个未完成 |
| 已封存canary `-02` | [config-02.json](canary/config-02.json) | 100/100 completed、0 unknown；冻结SER 4/100，离线语义复核10/100 |
| 早期安全替代BF1–BF4 | [benign/README](benign/README.md)、[config-05.json](benign/config-05.json) | 100/100 completed、0 unknown、冻结SER 9/100；改变过目标动作，单独保留 |

本轮在专属目录修正方法实现，使用新配置和新ID，旧原始输入与结果不覆盖。复用300条采集日志，不重解题、不重评分；具体进度、预算及论文偏移只在canary记录维护。

## 本实验的边界

```text
poisoned_evolution/
├── README.md              本实验入口
├── __main__.py            python -m 入口
├── spreadsheetbench/      专属采集、改写、检测、进化桥接和调度代码
├── tests/                 该实验的离线测试
├── canary/                新版F配置、结果说明与PPT提纲
└── benign/                早期BF冻结配置、任务选择与结果说明
```

后续复现修正只在这个目录进行。`scripts/injection/`、`scripts/wildclawbench/`、`sharedskill/`、根 `evolve_server/` 和此前三天三夜配置未因本次整理而修改。

本实验运行原生引擎 `runtime/poisoned-evolution/skillclaw-e9c745466f14/`，使用它自己的虚拟环境，不加载根目录已修改的进化服务。参考数据和S0继续从 `reproduction/reference/` 读取；大体积依赖不复制到实验目录。

冻结证据继续保存在 `artifacts/<ID>/`，原始日志和指标保存在 `outputs/<ID>/`；目录迁移没有改动既有实验ID、冻结JSON或结果。旧canary/benign配置中的历史目录名由[路径映射](spreadsheetbench/layout.py)解析，原始字符串保留；旧实验不能使用迁移后的源码继续运行。

## 命令

从仓库根目录执行：

```bash
.venv/bin/python -m experiments.poisoned_evolution --help
.venv/bin/python -m experiments.poisoned_evolution status
.venv/bin/python -m pytest experiments/poisoned_evolution/tests -q
```

不带配置的 `status` 默认查看canary `-05`；其他操作须显式提供配置。查看旧结果时指定相应历史配置。历史命令中的 `scripts.reproduction.poisoned_evolution_spreadsheetbench` 是旧位置，不再是执行入口。

已完成或用户终止的run在写状态前拒绝重启，`status`只读账本，终态`report`只返回已有报告。迁移已核对历史文件未变、此前三天三夜代码与公共库边界一致。

论文级方法与来源见[论文综述](../../docs/poisoned-evolution-experiments.md)，本次结果与限制见[canary记录](canary/README.md)，汇报材料见[PPT提纲](canary/PRESENTATION.md)。具体运行状态只在各版本README记录。
