# Shared-Skill 文档

从[根目录 README](../README.md)安装依赖后，可按下面的路径配置客户端和演化服务。具体组件与数据流见[系统架构](architecture.md)，来源和许可见[上游说明](upstream.md)。

## 本机共享技能与自动演化

下面使用两个服务和一个本地共享目录。先用 `sharedskill setup` 配好可用的模型后端，然后设置：

```bash
sharedskill config sharing.enabled true
sharedskill config sharing.backend local
sharedskill config sharing.local_root ~/.sharedskill/local-share
sharedskill config sharing.group_id local-demo
sharedskill config sharing.user_alias client-a
sharedskill config sharing.skill_reload_mode callback
sharedskill config evolve.server_url http://127.0.0.1:30001
sharedskill config evolve.proxy_reload_url http://127.0.0.1:30000
```

共享 group 的客户端和演化服务须使用相同的存储根目录与 group。不同机器之间需要可共同访问的存储；各自机器上的本地目录不会自动互通。

已有技能可先显式推送，再启动服务：

```bash
sharedskill skills push --no-filter
sharedskill start --port 30000
```

在另一个已激活 `.venv` 的终端启动工作流演化服务：

```bash
sharedskill-evolve-server --use-sharedskill-config --engine workflow \
  --port 30001 --interval 60 --publish-mode direct --skill-verifier
```

`--skill-verifier` 启用候选技能的 LLM 评审。`direct` 模式在当前启用的评审通过后直接发布；需要先收集客户端验证结果时使用 `--publish-mode validated`，并配置客户端 `validation.enabled`。两者的实际行为见[发布与治理](architecture.md#发布与治理)。

```bash
curl http://127.0.0.1:30001/status
curl -X POST http://127.0.0.1:30001/trigger
sharedskill skills pull
sharedskill validation status
sharedskill dashboard serve --host 127.0.0.1 --port 3788
```

客户端关闭会话时异步上传会话；演化服务轮询或接受 `/trigger` 请求。`sharing.session_upload_interval` 大于零时，还会按用户轮次上传快照并尝试触发演化。为便于精确归属，自己的客户端可以提供 `X-Session-Id`，并用 `X-Session-Done: true` 或请求字段 `session_done: true` 明确关闭会话。

## 本地良性验收

验收驱动会启动独立的 loopback 模型桥接、客户端代理和演化服务，创建三个 XLSX 测试样例，执行真实工具调用，检查发布版本与后续读取。需要已有可用的 Codex CLI 登录；运行会发起模型请求。

```bash
.venv/bin/python benign_validation/run_benign_e2e.py \
  --model gpt-5.6-sol --output /tmp/sharedskill-benign-demo
```

输出目录必须新建或为空。结果包括 `run.json`、任务请求和工具结果、上传会话、演化周期、版本 manifest、发布后的技能和模型调用日志。大体积运行证据保存在本地，不作为安装依赖或仓库必备文件。

## 实验与参考数据

- [原始 SkillClaw 实验设置](paper-experiments.md)：正常进化流程、8 用户和 6 轮参数、WildClawBench 来源与本地正常闭环验证。
- [PoisonedEvolution 实验设计](poisoned-evolution-experiments.md)：轨迹采集与投毒流程、模型角色、30/3 批次、600 次主实验、消融参数和两篇论文的设置区别。

[reproduction](../reproduction/README.md) 使用合成轨迹演示证据被写入技能的机制；它与真实良性任务验收分别记录。公开 benchmark 文件的来源由 [source-manifest.json](../reproduction/reference/source-manifest.json) 记录，大体积工作簿和下载档案需单独准备。参考数据下载完成不等于已生成模型执行日志，也不等于复现论文的数据划分。
