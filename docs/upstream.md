# 上游来源与项目边界

## 代码来源

Shared-Skill 的客户端代理、技能管理、共享存储、演化服务和相应测试基于 [AMAP-ML/SkillClaw](https://github.com/AMAP-ML/SkillClaw) 的公开实现。引入时固定的上游提交为：

```text
3938f7537645c961d94498a0a79fc0a977019595
```

[上游固定版本](https://github.com/AMAP-ML/SkillClaw/tree/3938f7537645c961d94498a0a79fc0a977019595)用于追溯来源。本项目在其基础上增加实验与验收驱动，并将客户端包、命令和默认配置命名为 `sharedskill`。仓库名称保持 `Shared-Skill`。

## 许可与归属

根目录 [LICENSE](../LICENSE) 保留上游 MIT 许可及原有版权声明。复用的源码中已有来源注释一并保留。相关上游功能与论文贡献归原作者；本项目文档描述的是当前仓库的实现和本地验证范围。

公开 SpreadsheetBench 数据及 Anthropic xlsx 技能是独立的第三方参考材料，适用各自许可证；根目录 MIT 许可不替代这些材料的许可。大体积下载档案与工作簿不随项目源码提交，来源 URL、固定提交和哈希记录在 [source-manifest.json](../reproduction/reference/source-manifest.json)。

## 命名与迁移

| 项目 | 当前入口 |
| --- | --- |
| GitHub 仓库与文档标题 | `Shared-Skill` |
| Python 发行包与客户端包 | `sharedskill` |
| 客户端命令 | `sharedskill` |
| 演化服务命令与包 | `sharedskill-evolve-server`、`evolve_server` |
| 通用配置与技能目录 | `~/.sharedskill/config.yaml`、`~/.sharedskill/skills` |
| 独立配置环境变量 | `SHAREDSKILL_CONFIG_FILE` |

原 `SkillClaw/` 内的源码已提升至仓库根目录；使用 `sharedskill/`、`evolve_server/`、`tests/`、`scripts/` 和根目录 `pyproject.toml`。迁移后的源码安装命令见[根 README](../README.md)。已有外部 Agent 仍使用自身的原生目录；历史实验日志保留当时的 `SkillClaw` 名称与路径，避免改写证据。

## 验证与论文范围

本仓库区分以下证据层级：

- **项目实现**：在上游基础上维护代理、技能库、共享存储和演化链路；功能以当前代码和测试为准。
- **良性闭环验收**：GPT 5.6 处理本项目生成的三个真实工作簿，运行实际工具、生成技能更新、发布并复用新版本；使用 Codex CLI 文本桥接，不提供 API 等价或论文任务准确率结论。
- **PoisonedEvolution 机制演示**：用合成轨迹与惰性 canary 测试 execution 阶段的技能规则写入，详见 [合成机制实验](../experiments/poisoned-evolution-mechanism/README.md)。它没有重现论文 600 次实验、91% SER 或实际攻击执行。

- **真实轨迹端到端实验**：在WildClawBench真实执行后改写上传轨迹副本，经过原生概要、SkillClaw进化、验证发布与后续隔离观测。具体参数与已覆盖范围由[实验README](../experiments/dymalskill-trajectory-e2e/README.md)和冻结证据定义，不把流程完成当作论文主张成立。

上游 SkillClaw 论文及 PoisonedEvolution 论文是不同研究。导入上游代码、完成数据下载或通过一次工作流验收，都不表示已复现任一论文的完整实验。
