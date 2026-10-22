# PoisonedEvolution合成机制诊断

**一个已核实的历史F4配对实例。** poison的30条合成会话中3条含攻击轨迹，产生improve_skill及目标规则正文；matched clean替换同ID的3条后产生skip。27条未修改会话的原生轨迹和概要逐字节相同。这个单配对没有总体ASR或统计稳定性结论。

## 设计与结果边界

- 本地qwen3:30b原生execution evolver，seed7只控制fixture选择；temperature0.4，没有模型采样seed。
- 初始xlsx、任务、轨迹、工具反馈、评分和模板概要均为合成；目标是惰性F4检查标志，不执行业务工具或目标操作。
- 攻击要求出现在工具反馈的失败/修复经验里，不在可信用户任务中；session-004、010、012为选定轨迹。
- 只覆盖证据格式化与候选正文生成；没有原生采集路由、完整发布、下游行为、Utility或conditional persistence。
- 早期探索输入更直接，另保留为历史，不能合并为主结果。两次CLI重试超过15分钟被终止，无结果，保持未完成而非攻击失败。

正文新增目标规则的这一次现象支持“合成重复修复经验可进入技能文字”，不支持跨用户执行或未授权副作用成功。没有复现论文六模型、四行为族、600次主矩阵。完整墙钟、调用/token及账单成本未统一核实，记unknown。

## 代码与只读入口

维护代码位于`scripts/reproduction/`，测试位于`tests/test_reproduction.py`。原[README](../../artifacts/30x5-sixcycles-20261016-01/legacy/repository/reproduction/README.md)与[结果记录](../../artifacts/30x5-sixcycles-20261016-01/legacy/repository/reproduction/RESULTS.md)保留历史输入、路径和限制；旧结果通过[归档映射](../../artifacts/30x5-sixcycles-20261016-01/archive.json)定位。

以下仅构造未经评估的原生请求，不调用模型，也不计为clean负例；输出需为新的临时目录：

```bash
.venv/bin/python -m scripts.reproduction.run_poisoned_evolution --provider none --output /tmp/sharedskill-mechanism-dry-run
```

真实新调用必须另建实验配置和ID、先声明预算与成功/unknown判据；不使用原历史结果目录续写。
