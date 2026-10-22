# 仓库工作规则

## 开始前

- 先读本文件、README 的当前实验和本次配置；涉及方法时再读 [INSTRUCTION](docs/INSTRUCTION.md)。不要从 IDE 打开的单个文件推断实验。
- 保持代码和文档简洁，不加无关脚手架、重复报告或逐文件哈希清单；未经用户明确要求，不 commit/push。
- 独立论文复现的专用代码、测试、配置和说明集中在所属实验目录；不得顺带修改其他实验或公共引擎。PoisonedEvolution 的边界是 `experiments/poisoned_evolution/`。
- 实验设计、实验运行、架构与目录整理、代码组织、文档等工作，都按需要主动选择或组合成熟、可靠的 skill，不固定名称，也不必等遇到困难才使用。先读 skill，只采用相关步骤，用户约束优先。

## 实验中

- 付费运行前写清研究问题、指标分母、成功/unknown 判据、停止条件和墙钟/调用/token 预算；先过真实端到端 smoke，再跑矩阵。
- 配置字段必须实际生效并有测试；运行后冻结配置，改动另用新实验 ID。设置 command、agent、case 和整轮硬上限及早停条件。
- Utility 取 reward，ASR 看真实副作用；只有 baseline ASR=true 才能测 conditional persistence。计分完成前超时，两项均 unknown；collector 单独失败只让 ASR unknown。
- 区分任务失败、verifier、provider 和基础设施错误；同一基础设施错误签名连续两次即熔断，不作为改 Skill 的反馈。发现方法错误先停付费工作、修复再运行。
- 复用有效证据，不盲目重跑超时或已完成的 final。final 只评一次 exact selected snapshot；snapshot 与部署副本用 digest 校验，不能比较路径字符串。
- 隔离 case 后动态并发；必须串行时说明原因。监控增量读取日志，避免重复解析、哈希和模型调用。
- 不浪费 token。

## 结束后

- `artifacts/<ID>/` 放配置、源码、快照和证据；`outputs/<ID>/` 只放指标与原始日志；`runtime/` 只留当前引擎和依赖，历史副本先归档再清理。
- 密钥只由入口加载，不打印或写入产物；清除 runtime 中 `.venv` 以外的 Python、pytest 和 ruff 缓存。
- 只在 README 更新状态、入口、配置/产物路径、指标分母与 unknown、耗时/调用/token、异常和浪费；区分完成、失败、未覆盖，不把流程跑通写成研究结论。
