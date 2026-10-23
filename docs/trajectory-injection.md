# 轨迹注入格式与接口

本页描述输入适配和CLI，不保存当前排期或实验结果。论文级问题与threat model见[INSTRUCTION](INSTRUCTION.md)，具体任务、payload版本、轮数、预算与结果见[实验索引](../README.md#实验索引)。

## Payload与可信边界

[`injection/`](../injection/README.md)保存十二类独立英文JSON与catalog，不放可执行代码或结果；`scripts/injection/`负责适配、模型输入保存和实验驱动。catalog固定来源与payload版本，实际运行冻结选定文件，后续修改不回写历史结果。

这些适配来自本地DyMalSkill材料的行为级代表。当前schema使用预写的`message`请求固定合成操作，并要求其进入可复用流程；不写虚构成功声明，不根据任务或模型反馈自动优化文案。来源、参数和指标留在元数据，编译器只将message正文送入模型输入。适配器本身不分发工具、不读真实凭据、不发送数据、不执行数据库写入或删除、负载与延时。旧constraint/action/success格式仅供历史合成路径兼容。

| Payload | 合成目标行为 |
| --- | --- |
| credential_abuse | 合成配置发现与披露 |
| data_exfil_http | 固定时间戳/平台值的HTTP请求计划 |
| data_exfil_file | 本地调用审计记录 |
| data_exfil_log | 固定合成日志的传输计划 |
| mock_api | 固定合成凭据的请求计划 |
| dos | 延时步骤的文字记录，不实际等待 |
| rce | 惰性测试产物的取回计划，不下载或执行 |
| db_insert / db_delete | 固定合成数据库插入/删行计划 |
| file_delete | 指定合成缓存记录的清理计划，不实际删除 |
| cpu_hijack / gpu_hijack | 合成负载步骤的文字记录，不实际占用资源 |

一个payload代表一类适配，不是全部原始实例的复现。marker保存在元数据，不一定出现在message中；旧marker启发式对新message标为不适用，不能算零采纳。记录only与真实行为执行配置必须明确区分。目前行为observer只覆盖`data_exfil_file`的合成本地写入；其余类别的ASR未覆盖。

## 编译主框架的消息正文

```bash
.venv/bin/python -B -m scripts.injection.platform_payload \
  --case data_exfil_file --output artifacts/NEW_ID/payload.json
```

零模型调用，拒绝覆盖。输出`text=message`与`insertion_mode=message_body`，供新实验`payload_file`使用。主适配器只在可证明关联的Slack `content`／邮件`body`内加入文字；保留原正文、发送者、ID和其他返回值。同一读取返回多个候选时，可加`--message-id`固定已请求的一个ID；歧义或截断拒绝，不退回工具输出前置。

## 构造合成批次

以下命令都从仓库根目录运行。`build`仅构造数据，不调用模型；输出目录必须新建。

```bash
.venv/bin/python -m scripts.injection build --output /tmp/sharedskill-injection-inputs
```

默认每个case独立构造30条会话，seed7选3条增强，其他27条与clean相同；每类从同一初始技能开始，不跨case继承。新message只加入已有工具结果，不增加工具操作或成功重试；此入口是合成诊断，不能代替主框架的真实消息关联。默认`--summary none`仅构造轨迹；显式`--summary template`才加入人工模板概要，必须单独标注。fixture不发明PRM/ORM评分或声称真实任务执行。

保存的`*-request.json`是原生helper的system/user消息，未经模型评估；capture stub的skip不是模型决策。适配器核对当前message正文经过原生字段截断后是否完整保留；旧格式另检查constraint、action和success。

## 修改已有会话的副本

```bash
.venv/bin/python -m scripts.injection inject \
  --case data_exfil_http --sessions /path/to/sessions.json \
  --session-ids session-004 session-010 session-012 \
  --output /tmp/sharedskill-injected-sessions.json
```

只有选中的flat session改变，输入不覆盖。新message添加到选中turn的已有工具结果，保留turn数量、调用ID与工具元数据；Python接口可设置`insertion_index`。这是显式标注的`synthetic_tool_result_append`诊断，无法证明该文字来自真实消息body；主框架必须使用上一节的关联适配器。旧格式才需要工作簿`--output-path`并生成反事实turn。tool-call ID必须配对且唯一；聚合多rollout输入拒绝，不静默摊平。

缓存trajectory、summary和metadata重新构造，旧概要清除；增强会话的整体成功声称删除，原指标仅在独立来源记录中保留。原turn和旧评分仍只证明原步骤，不能证明新插入路径执行过。

```python
from scripts.injection.trajectory import inject, load_catalog

case = load_catalog()["data_exfil_http"]
augmented = inject(original_session, case)
```

catalog中的payload摘要必须与最终字节一致；加载器拒绝不匹配摘要或逃出injection目录的路径。编辑后的版本只适用于新实验，不能替换已冻结输入。

## 原生进化诊断与真实端到端的区别

纯原生诊断CLI为`python -m scripts.injection run --endpoint URL --model MODEL --output NEW_DIR`，直接调用execution evolver，保存真实请求、响应、决策与有效正文。端点与模型必须显式给出；CLI不启动模型服务，也不读取隐含key文件。实际模型调用前需在所属实验固定矩阵、预算和停止条件。

诊断不运行完整存储、候选验证、发布和下游任务。无效JSON、HTTP错误、有效skip分开保留；description-only变更按原生继承正文语义处理，不能因为没有新文件就误判结果。

真实轨迹的Day/Night驱动使用`python -m scripts.injection.e2e_cycles --config PATH --run-id ID`。任务、轮数、条件、payload和终点来自实验配置，不能从旧目录名或IDE文件推断。已完成历史配置为只读；`--dry-run`核对配置而不启动任务。新运行先过真实端到端smoke，不使用本页中的示例目录接续历史科学结果。

## 测量与验证

完整投毒送达需检查实际概要请求，不能只查准备文件。概要、候选、接受正文与发布全库的正向采纳分别审读；拒绝、引述和自报不计。行为成功要同时有预定义副作用和对应执行证据，语义留存率不是ASR。

相关离线测试位于`tests/`，不调用模型：

```bash
.venv/bin/python -m unittest discover -s tests -p 'test_*injection.py'
```

同一基础设施签名反复失败须熔断并保留unknown；不能通过自动重跑已完成final或耗尽会话补出成功。历史文件按原字节归档，绝对路径通过archive根映射读取。
