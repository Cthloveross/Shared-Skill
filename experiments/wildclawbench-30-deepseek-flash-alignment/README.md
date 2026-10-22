# WildClawBench机制对齐诊断

**总交付finished_with_gaps。** v1完成12/12任务诊断及一次原生整夜进化；v2只完成三项窄修复验收与prepare，没有追加任务执行、候选A/B或发布。配置中继承的六日六夜字段不是该诊断的待运行排期。

## 设计与配置

[只读配置](config.json)、[原30题选择](tasks.json)保留参数；实际诊断仅01.02、02.11、03.01、04.04、05.11、06.07六题，每题各用历史S0/S6一次。原生进化只输入旧运行完整30份Day1会话与S0。历史S6含旧人工修订，不能当作本次进化产物。

执行/进化为本地Qwen3.8-27B，外部DeepSeek Flash评分；seed42、temperature1、top_p0.95、输出16384、上下文131072；付费上限$1。v1以整夜bundle作为原子变更集，任何来源不可核对就整批隔离。这是本地选择，不是论文强制门槛。

## 结果、unknown与边界

| 项目 | 已核实结果 |
| --- | --- |
| 诊断覆盖 | 12/12有效数值，6/6配对，无缺分 |
| 六题历史库均值（reward×100） | S0 54.590，S6 63.735，差+9.145；两升两平两降 |
| 诊断墙钟 | 19分10.87秒 |
| 原生进化 | 21分46.92秒；其中Agent进程14分28.05秒、rc0 |
| 原生候选 | 8个bundle（6 refine、2 create），全部隔离unverifiable_sources |
| 正常通道候选A/B/发布 | 0/0/0；不等于原生生成失败 |
| 付费 | 6次，费用上界$0.014248236/$1，无pending或缺usage |

一次Agent会话包含多轮模型请求，不能记为总共一次模型调用；完整token和全部请求总数未统一核实，记unknown。这里没有行为ASR、conditional persistence或新演化Utility结论。

来源声明错误包括不存在的任务标签、伪造或变长session ID；没有猜测或改写history放行。原bundle机械加载检查通过，仅证明可加载，不取消来源隔离。

v1空WAV的0帧输入错误地转发到后端，连续503，合同验收失败；真实05.11评分84.71保留。v2改为推理前400、允许合法支持文件删除并防止残留、收紧完成缓存的文件/目录/模式校验。241项离线测试、部署检查与23次真实传输smoke通过；smoke为0 Agent、0付费，仅2次本地工具推理。v2 reviewed仅prepare，0任务结果、无原生调用，不能继承v1分数。

## 证据与入口

- [原实验记录](../../artifacts/30x5-sixcycles-20261016-01/legacy/repository/experiment/reproduction/wildclawbench-30-deepseek-flash-alignment/EXPERIMENT.md)、[总交付索引](../../artifacts/30x5-sixcycles-20261016-01/legacy/repository/experiment/reproduction/wildclawbench-30-deepseek-flash-alignment/runtime/validation/final-acceptance.json)、[v1接受摘要](../../artifacts/30x5-sixcycles-20261016-01/legacy/repository/experiment/reproduction/wildclawbench-30-deepseek-flash-alignment/runtime/runs/20261016-alignment-v1/acceptance-summary.json)、[v1来源与诊断终审](../../artifacts/30x5-sixcycles-20261016-01/legacy/repository/experiment/reproduction/wildclawbench-30-deepseek-flash-alignment/runtime/runs/20261016-alignment-v1/audits/v1-final-native-and-diagnostic-integrity.json)。
- [原机制矩阵](../../artifacts/30x5-sixcycles-20261016-01/legacy/repository/experiment/reproduction/wildclawbench-30-deepseek-flash-alignment/mechanism-matrix.md)区分论文、公开实现和本地选择；[归档根映射](../../artifacts/30x5-sixcycles-20261016-01/archive.json)。
- 维护代码已移至scripts/wildclawbench，历史工作树只作为冻结来源归档，不再动态加载。只读配置可`--dry-run`检查；不启动新诊断或重新评分。
