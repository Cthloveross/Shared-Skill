# Shared-Skill

Shared-Skill 是一个 Agent 技能管理与演化项目。它在模型请求代理中记录实际会话、跟踪技能使用，将这些证据交给独立的演化服务，生成技能更新，再通过共享存储分发到客户端。

Python 包和命令使用 `sharedskill`；仓库名称保持 `Shared-Skill`。

## 快速启动

需要 Python 3.10+、uv，以及可用的模型后端。在仓库根目录执行：

```bash
uv venv .venv
uv pip install --python .venv/bin/python -e '.[server,validation]'
source .venv/bin/activate
sharedskill setup
sharedskill config proxy.host 127.0.0.1
sharedskill start --port 30000
```

`setup` 配置模型地址、模型名称、技能目录、可选 PRM 评分和共享存储。首次使用可选择 `claw_type=none`，先接入自己的 OpenAI 兼容客户端。另一个终端可检查：

```bash
curl http://127.0.0.1:30000/healthz
```

客户端配置默认保存到 `~/.sharedskill/config.yaml`；`SHAREDSKILL_CONFIG_FILE` 可以指定独立配置。通用技能目录为 `~/.sharedskill/skills`，选用外部 Agent 集成时可使用该 Agent 的原生技能目录。

## 使用路径

| 需求 | 入口 |
| --- | --- |
| 转发模型请求、注入技能、记录会话 | `sharedskill start` |
| 检查或恢复外部 Agent 配置 | `sharedskill doctor`、`sharedskill restore` |
| 显式推送、拉取与同步技能 | `sharedskill skills` |
| 基于会话生成技能更新 | `sharedskill-evolve-server` |
| 查看技能、会话、版本和验证任务 | `sharedskill dashboard` |
| 本地真实 XLSX 任务闭环验收 | [benign_validation/run_benign_e2e.py](benign_validation/run_benign_e2e.py) |
| PoisonedEvolution 合成证据实验 | [reproduction/README.md](reproduction/README.md) |

代理提供 Chat Completions、Responses 和 Anthropic Messages 接口。自动演化需要客户端与演化服务访问同一个共享存储和 group；存储可以是本地目录、S3 兼容服务或阿里云 OSS，技能注册还可接入 Nacos。

## 文档与证据

- [部署与操作入口](docs/README.md)：本地共享存储配置、演化服务启动和验证命令。
- [系统架构](docs/architecture.md)：任务执行、会话演化、存储结构、版本和发布路径。
- [原始 SkillClaw 实验设置](docs/paper-experiments.md)：正常进化的流程、WildClawBench 来源与本地验证范围。
- [PoisonedEvolution 实验设计](docs/poisoned-evolution-experiments.md)：投毒参数、SpreadsheetBench 轨迹、结果与两篇论文的设置对照。
- [上游来源与项目边界](docs/upstream.md)：代码来源、MIT 许可、命名变化和实验范围。

本地已完成一次 GPT 5.6 的良性闭环验收：两条真实工具执行会话生成技能 v2，第三个任务读取并复用该版本。工作簿是本项目生成的测试样例；模型通过 Codex CLI 桥接访问，消息角色被转换为文本。这验证流程能运行，不提供论文任务准确率或 API 等价结论。运行说明与局限见[架构文档的验收部分](docs/architecture.md#良性任务闭环验收)。

项目基于 [AMAP-ML/SkillClaw](https://github.com/AMAP-ML/SkillClaw) 的公开代码发展，保留上游 [MIT 许可](LICENSE) 和来源说明。本仓库的合成投毒实验也不代表复现了 PoisonedEvolution 论文的 600 次实验或 91% 结果。
