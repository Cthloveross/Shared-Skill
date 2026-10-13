"""Reports from measured results only; incomplete stages retain their denominators."""

from __future__ import annotations

import csv
import datetime as dt
import json
import statistics
from collections import defaultdict
from pathlib import Path

from protocol import ROOT, fixed_execution_count, tree_hash, write_json


def backend_usage(directory: Path) -> dict | None:
    """Count Qwen usage from backend responses, including reasoning exactly once."""
    measured = []
    requests = 0
    for path in directory.glob("*/record.json"):
        record = json.loads(path.read_text())
        if record.get("role") != "main":
            continue
        requests += 1
        response = path.parent / "response.bin"
        stream = path.parent / "response.sse"
        usage = None
        try:
            if response.exists():
                usage = json.loads(response.read_bytes()).get("usage")
            elif stream.exists():
                for line in stream.read_text().splitlines():
                    if line.startswith("data: ") and line != "data: [DONE]":
                        payload = json.loads(line[6:])
                        if payload.get("usage"):
                            usage = payload["usage"]
        except (ValueError, UnicodeError):
            # A cancelled stream can end mid-JSON and have no final usage chunk.
            pass
        if isinstance(usage, dict) and all(
            isinstance(usage.get(k), int) for k in ("prompt_tokens", "completion_tokens", "total_tokens")
        ):
            measured.append(usage)
    if not requests:
        return None
    return {
        **{
            key: sum(u[source] for u in measured) if measured else None
            for key, source in (
                ("input_tokens", "prompt_tokens"),
                ("output_tokens", "completion_tokens"),
                ("total_tokens", "total_tokens"),
            )
        },
        "request_count": requests,
        "measured_request_count": len(measured),
        "missing_usage_requests": requests - len(measured),
    }


def wall_span(records: list[dict]) -> float | None:
    if not records:
        return None
    starts = [dt.datetime.fromisoformat(r["started_at"]) for r in records if r.get("started_at")]
    ends = [dt.datetime.fromisoformat(r["finished_at"]) for r in records if r.get("finished_at")]
    return (max(ends) - min(starts)).total_seconds() if starts and ends else None


def summarize(records: list[dict]) -> dict:
    graded = [r for r in records if r.get("status") == "completed" and r.get("score") is not None]
    values = [r["score"] for r in graded]
    mean_score = statistics.mean(values) if values else None
    usage = [r["usage"] for r in records if isinstance(r.get("usage"), dict)]
    skill_reads = [
        r["session"]["read_skills"] for r in records if isinstance((r.get("session") or {}).get("read_skills"), list)
    ]
    return {
        "attempted": len(records),
        "graded": len(graded),
        "score": mean_score,
        "mean_task_score_0_to_100": mean_score * 100 if mean_score is not None else None,
        "zero_score_tasks": sum(value == 0 for value in values),
        "full_credit_tasks": sum(value == 1 for value in values),
        "skill_read_records": len(skill_reads),
        "shared_skill_tasks": sum(bool(reads) for reads in skill_reads) if skill_reads else None,
        "shared_skill_names": sorted({name for reads in skill_reads for name in reads}) if skill_reads else None,
        "task_seconds": sum(r.get("wall_seconds", 0) for r in records),
        "timeouts": sum(r.get("timed_out", False) for r in records),
        "tool_errors": sum(r.get("tool_errors", 0) for r in records),
        "wall_span_seconds": wall_span(records),
        "usage_records": len(usage),
        "usage": {
            key: sum(u[key] for u in usage if u.get(key) is not None)
            if any(u.get(key) is not None for u in usage)
            else None
            for key in (
                "input_tokens",
                "output_tokens",
                "total_tokens",
                "request_count",
                "measured_request_count",
                "missing_usage_requests",
            )
        },
    }


def report_owner(run_dir: Path, config: dict) -> Path | None:
    """An experiment document belongs to its data folder, never its engine checkout."""
    directory = run_dir.resolve()
    if directory.parent.name != "runs" or directory.parent.parent.name != "runtime":
        if config.get("experiment_root"):
            raise ValueError("Experiment reports require <experiment>/runtime/runs/<run_id>")
        return None  # Standalone diagnostic reports cannot update an experiment document.
    owner = directory.parent.parent.parent
    if config.get("experiment_root") and Path(config["experiment_root"]).resolve() != owner:
        raise ValueError("Report run belongs to a different experiment_root")
    if config.get("runtime_dir") and Path(config["runtime_dir"]).resolve() != owner / "runtime":
        raise ValueError("Report runtime belongs to a different experiment folder")
    return owner


def generate(run_dir: Path, tasks: list[dict]) -> dict:
    output = run_dir / "report"
    frozen_path = run_dir / "frozen-protocol.json"
    config = json.loads(frozen_path.read_text())["config"] if frozen_path.exists() else {}
    owner = report_owner(run_dir, config)
    output.mkdir(parents=True, exist_ok=True)
    task_count = len(tasks)
    unchanged_count = sum(not task.get("local_variant", False) for task in tasks)
    runtime_dir = Path(config.get("runtime_dir") or (owner / "runtime" if owner else run_dir))
    evidence = Path(config.get("gateway_log_dir") or runtime_dir / "api-evidence")
    records = []
    for path in sorted((run_dir / "executions").glob("*/*/*/*/*/result.json")):
        result = json.loads(path.read_text())
        if result.get("experiment_record"):
            result["usage"] = backend_usage(evidence / result["run_id"]) if result.get("run_id") else None
            result["usage_source"] = "gateway_main" if result["usage"] is not None else "unavailable"
            records.append(result)
    fields = [
        "phase",
        "round",
        "arm",
        "candidate_id",
        "user_id",
        "task_id",
        "short_id",
        "category",
        "skill_bank_sha256",
        "status",
        "score",
        "timed_out",
        "tool_errors",
        "wall_seconds",
        "started_at",
        "finished_at",
        "output_dir",
        "input_tokens",
        "output_tokens",
        "total_tokens",
        "request_count",
        "measured_request_count",
        "missing_usage_requests",
        "usage_source",
    ]
    with (output / "tasks.csv").open("w") as stream:
        writer = csv.DictWriter(stream, fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({**record, **(record.get("usage") or {})} for record in records)
    groups = defaultdict(list)
    for record in records:
        if record["phase"] in {"day", "endpoint", "pilot"}:
            groups[(record["phase"], record["round"], record.get("arm", ""))].append(record)
    stages = []
    for (phase, number, arm), rows in sorted(groups.items()):
        total = summarize(rows)
        expected_tasks = [
            task for task in tasks if phase != "pilot" or task["short_id"] in config.get("pilot_short_ids", [])
        ]
        expected = len(expected_tasks)
        by_category = {}
        for category in sorted({t["category"] for t in tasks}):
            category_rows = [r for r in rows if r["category"] == category]
            by_category[category] = {
                **summarize(category_rows),
                "expected": sum(t["category"] == category for t in expected_tasks),
            }
        stages.append(
            {
                "phase": phase,
                "round": number,
                "arm": arm,
                **total,
                "expected": expected,
                "grading_coverage": total["graded"] / expected if expected else None,
                "full_credit_rate": total["full_credit_tasks"] / expected
                if expected and total["graded"] == expected
                else None,
                "categories": by_category,
            }
        )
    paired = []
    control = {r["task_id"]: r for r in records if r["phase"] == "endpoint" and r.get("arm") == "S0"}
    final = {r["task_id"]: r for r in records if r["phase"] == "endpoint" and r.get("arm") == "S6"}
    for task in tasks:
        task_id = task["task_id"]
        if task_id in control and task_id in final:
            old, new = control[task_id], final[task_id]
            valid = (
                old.get("status") == new.get("status") == "completed"
                and old.get("score") is not None
                and new.get("score") is not None
            )
            paired.append(
                {
                    "task_id": task_id,
                    "short_id": task["short_id"],
                    "category": task["category"],
                    "local_variant": task["local_variant"],
                    "S0": old.get("score"),
                    "S6": new.get("score"),
                    "delta": new["score"] - old["score"] if valid else None,
                }
            )
    with (output / "endpoint-paired.csv").open("w") as stream:
        writer = csv.DictWriter(stream, ["task_id", "short_id", "category", "local_variant", "S0", "S6", "delta"])
        writer.writeheader()
        writer.writerows(paired)
    decisions = [
        json.loads(path.read_text()) for path in sorted((run_dir / "nights").glob("*/validation/*/decision.json"))
    ]
    valid_pairs = [p for p in paired if p["delta"] is not None]
    unchanged = [p for p in valid_pairs if not p["local_variant"]]
    state = json.loads((run_dir / "state.json").read_text()) if (run_dir / "state.json").exists() else {}
    paid_ledger_path = runtime_dir / "paid-api/budget.json"
    paid_api = json.loads(paid_ledger_path.read_text()) if config.get("external_judge") and paid_ledger_path.exists() else None
    phase_totals = {
        phase: summarize([r for r in records if r["phase"] == phase]) for phase in sorted({r["phase"] for r in records})
    }
    nights = []
    for path in sorted((run_dir / "nights").glob("*/publication.json")):
        publication = json.loads(path.read_text())
        proposal_path = path.parent / "proposals/evolution/proposal-summary.json"
        proposal = json.loads(proposal_path.read_text()) if proposal_path.exists() else {}
        related_records = [r for r in records if r["phase"] == "validation" and r["round"] == publication["round"]]
        # A candidate ID can recur on another night; include its parent hash in the per-night decision provenance.
        night_decisions = [
            json.loads(p.read_text()) for p in sorted((path.parent / "validation").glob("*/decision.json"))
        ]
        parent_bank = run_dir / "banks" / f"S{publication['round'] - 1}"
        current_bank = run_dir / "banks" / f"S{publication['round']}"
        parent_skills = {p.name: tree_hash(p) for p in parent_bank.iterdir() if p.is_dir()}
        current_skills = {p.name: tree_hash(p) for p in current_bank.iterdir() if p.is_dir()}
        nights.append(
            {
                **publication,
                "proposal_seconds": proposal.get("elapsed_seconds"),
                "summary_seconds": proposal.get("summarization_elapsed_seconds"),
                "validation_executions": len(related_records),
                "validation_task_seconds": sum(r["wall_seconds"] for r in related_records),
                "accepted_names": [d["name"] for d in night_decisions if d.get("accepted")],
                "rejected_names": [d["name"] for d in night_decisions if not d.get("accepted")],
                "skill_count": len(current_skills),
                "new_skills": sorted(set(current_skills) - set(parent_skills)),
                "changed_skills": sorted(
                    name
                    for name in current_skills.keys() & parent_skills.keys()
                    if current_skills[name] != parent_skills[name]
                ),
            }
        )
    with (output / "skill-lineage.csv").open("w") as stream:
        writer = csv.DictWriter(
            stream, ["round", "parent_sha256", "bank_sha256", "candidate_count", "timestamp"], extrasaction="ignore"
        )
        writer.writeheader()
        writer.writerows(nights)
    ocr_records = [
        {
            "phase": r["phase"],
            "round": r["round"],
            "arm": r.get("arm"),
            "status": r["status"],
            "completion_score": r.get("score"),
            "model_ocr_accuracy": r.get("scores", {}).get("ocr_accuracy"),
            "model_ocr_results": r.get("scores", {}).get("ocr_model_results"),
            "verified_samples": r.get("scores", {}).get("verified_samples"),
        }
        for r in records
        if r["short_id"] == "02.06"
    ]
    write_json(output / "ocr-variant.json", ocr_records)
    pilot_path = run_dir / "pilot-gate.json"
    pilot_calibration = json.loads(pilot_path.read_text()) if pilot_path.exists() else None
    initial_bank, final_bank = run_dir / "banks/S0", run_dir / "banks/S6"
    bank_changed = tree_hash(initial_bank) != tree_hash(final_bank) if final_bank.is_dir() else None
    summary = {
        "paid_api": paid_api,
        "reported_at": dt.datetime.now(dt.timezone.utc).isoformat(),
        "primary_metric": "mean_task_score_0_to_100",
        "experiment_root": str(owner) if owner else None,
        "expected_tasks": task_count,
        "expected_unchanged_prompt_tasks": unchanged_count,
        "fixed_execution_count": fixed_execution_count(tasks, config),
        "binary_task_success_rate": None,
        "state": state,
        "stages": stages,
        "candidate_accepts": sum(d.get("accepted", False) for d in decisions),
        "candidate_rejects": sum(not d.get("accepted", False) for d in decisions),
        "paired_graded": len(valid_pairs),
        "endpoint_delta": statistics.mean(p["delta"] for p in valid_pairs) if valid_pairs else None,
        "unchanged_prompt_pairs": len(unchanged),
        "unchanged_prompt_delta": statistics.mean(p["delta"] for p in unchanged) if unchanged else None,
        "ocr_variant": next((p for p in paired if p["local_variant"]), None),
        "records": len(records),
        "phase_totals": phase_totals,
        "nights": nights,
        "execution_wall_span_seconds": wall_span(records),
        "formal_wall_span_seconds": wall_span([r for r in records if r["phase"] in {"day", "validation", "endpoint"}]),
        "ocr_task_results": ocr_records,
        "pilot_calibration": pilot_calibration,
        "final_bank_changed": bank_changed,
    }
    write_json(output / "summary.json", summary)
    lines = [
        "# 实验运行结果",
        "",
        f"运行目录：`{run_dir.name}`；状态：**{state.get('status', 'not_started')}**。",
        "",
        "主指标：任务完成质量均分，等于有效 overall_score 的均值 × 100，单位为分（0–100）。",
        "status=completed 只表示执行和评分结束，包含零分失败与超时任务；评分覆盖率不等于任务成功率。",
        "满分任务按 score=1 统计，作为辅助描述；基准没有统一二元成功阈值，整题成功率保持未定义。",
        "缺失评分保留为空，不填零；未达到完整题数的均分标为部分结果，不作为完整阶段成绩。",
        "",
        "| 阶段 | 库／轮次 | 已执行 | 评分覆盖 | 完成质量均分（0–100） | 满分／已评分 | 累计任务小时 | 超时 |",
        "|---|---|---:|---:|---:|---:|---:|---:|",
    ]
    for stage in stages:
        score = f"{stage['score'] * 100:.2f}" if stage["score"] is not None else "待评分"
        if stage["graded"] != stage["expected"] and stage["score"] is not None:
            score += "（部分）"
        lines.append(
            f"| {stage['phase']} | {stage['arm'] or stage['round']} | {stage['attempted']} | "
            f"{stage['graded']}/{stage['expected']} | {score} | "
            f"{stage['full_credit_tasks']}/{stage['graded']} | "
            f"{stage['task_seconds'] / 3600:.2f} | {stage['timeouts']} |"
        )
    lines += [
        "",
        "技能使用与候选决策用于解释任务成绩变化，不作为主成绩：",
        "",
        "| 阶段 | 库／轮次 | 有技能读取记录的执行 | 读取共享技能的任务 | 不同共享技能 |",
        "|---|---|---:|---:|---|",
    ]
    for stage in stages:
        names = stage["shared_skill_names"]
        shared_tasks = stage["shared_skill_tasks"]
        lines.append(
            f"| {stage['phase']} | {stage['arm'] or stage['round']} | {stage['skill_read_records']} | "
            f"{shared_tasks if shared_tasks is not None else '未记录'} | "
            f"{(', '.join(names) or '无') if names is not None else '未记录'} |"
        )
    if pilot_calibration:
        lines += [
            "",
            f"预跑实际墙钟 {pilot_calibration['wall_seconds'] / 60:.1f} 分钟；"
            f"按这 {pilot_calibration.get('tasks', len(config.get('pilot_short_ids', [])))} 题外推固定 "
            f"{summary['fixed_execution_count']} 次为 {pilot_calibration['extrapolated_fixed_wall_hours']:.1f} 小时。",
            f"预跑样本外推只供排期。待 Day 1 全 {task_count} 题和首个夜晚完成后更新；不作为总实验完成时间。",
        ]
    lines += [
        "",
        f"候选接受 {summary['candidate_accepts']} 个，拒绝 {summary['candidate_rejects']} 个。",
        f"终点配对有效评分 {len(valid_pairs)}/{task_count}；其中保留原题提示 {len(unchanged)}/{unchanged_count}。",
        "",
    ]
    if task_count and len(valid_pairs) == task_count:
        lines.append(
            f"终点完成质量均分 S6−S0：{summary['endpoint_delta'] * 100:+.2f} 分（0–100）。"
        )
        if unchanged:
            lines.append(f"{unchanged_count} 道保留原提示题：{summary['unchanged_prompt_delta'] * 100:+.2f} 分。")
        if bank_changed is False:
            lines.append("S6 与 S0 的文件 hash 相同；上述差值属于同库重复测量，不能归因于技能内容进化。")
    else:
        lines.append("终点对照尚未完成，不能给出最终技能进步幅度。")
    lines += [
        "",
        "| 工作类型 | 执行次数 | 累计任务小时 | 阶段墙钟小时 | 已计量主模型 token |",
        "|---|---:|---:|---:|---:|",
    ]
    for phase, totals in phase_totals.items():
        span = f"{totals['wall_span_seconds'] / 3600:.2f}" if totals["wall_span_seconds"] is not None else ""
        tokens = totals["usage"]["total_tokens"]
        lines.append(f"| {phase} | {totals['attempted']} | {totals['task_seconds'] / 3600:.2f} | {span} | {tokens} |")
    lines += [
        "",
        "Token 使用路由后端 main 角色的 usage；reasoning 只计一次。",
        "中断请求可能没有最终 usage，计量缺口见 missing_usage_requests；已计量 token 不能当成全部消耗。",
        "此表不含夜间摘要／提案／比较或音频、图片辅助模型；原始 OpenClaw usage 保留在执行记录，未经改写。",
        "夜间摘要／提案耗时和接受技能见 summary.json 的 nights；版本 hash 与父版本另见 skill-lineage.csv。",
        "OCR 变体的任务完成分与模型 OCR 准确率分别记录，不能混为同一个分数。",
        "",
        "[逐题记录](tasks.csv) · [终点配对](endpoint-paired.csv) · [技能版本](skill-lineage.csv) · "
        "[OCR 变体](ocr-variant.json) · [机器可读汇总](summary.json)",
    ]
    if paid_api:
        reserved = sum(paid_api["pending"].values())
        lines += ["", f"DeepSeek 已结算请求 {paid_api['requests_settled']} 次；"
                  f"峰时费用上界 ${paid_api['cost_upper_bound_usd']:.4f}，"
                  f"未结算预留 ${reserved:.4f}，预算 ${paid_api['budget_usd']:.2f}。",
                  f"缺失有效 usage 的请求 {paid_api['missing_usage_requests']} 次，按最坏费用计入；"
                  "这是实验 router 全生命周期的保守账本，含接口预检、重试与比较，不是供应商实际账单。"]
    # Standalone figure, not a Mermaid chart or screenshot.
    day_stages = sorted(
        (s for s in stages if s["phase"] == "day" and s["expected"] and s["graded"] == s["expected"]),
        key=lambda s: s["round"],
    )
    if day_stages:
        import matplotlib

        matplotlib.use("Agg")
        import matplotlib.pyplot as plt

        fig, axes = plt.subplots(2, 3, figsize=(15, 8), sharex=True, sharey=True)
        for axis, category in zip(axes.flat, sorted({t["category"] for t in tasks})):
            axis.plot(
                [s["round"] for s in day_stages],
                [s["categories"][category]["score"] * 100 for s in day_stages],
                marker="o",
            )
            axis.set_title(category.replace("_", " "), fontsize=10)
            axis.set_xlabel("Day (Day 6 uses S5)")
            axis.set_ylabel("Mean task score (0–100)")
            axis.set_ylim(0, 100)
            axis.set_xticks(range(1, 7))
            axis.grid(alpha=0.25)
        fig.tight_layout()
        fig.savefig(output / "six-category-curves.png", dpi=180)
        fig.savefig(output / "six-category-curves.pdf")
        plt.close(fig)
        lines += ["", "![六领域逐日成绩](six-category-curves.png)"]
    (output / "REPORT.md").write_text("\n".join(lines) + "\n")
    update_experiment_status(run_dir, summary)
    return summary


def update_experiment_status(run_dir: Path, summary: dict):
    root = summary.get("experiment_root")
    if not root:
        return
    owner = report_owner(run_dir, {"experiment_root": root})
    document = owner / "EXPERIMENT.md"
    if not document.exists():
        return
    if document.resolve().parent != owner:
        raise ValueError("Experiment document must stay in its owning folder")
    latest_run = owner / "runtime/latest-run.txt"
    if not latest_run.exists() or Path(latest_run.read_text().strip()).resolve() != run_dir.resolve():
        return  # Historical reports stay under their own run; they cannot replace current status.
    marker = "<!-- LIVE_STATUS -->"
    text = document.read_text().split(marker)[0].rstrip()
    state = summary["state"]
    latest = [
        "",
        "",
        marker,
        "## 当前运行记录",
        "",
        f"- 状态：`{state.get('status', 'not_started')}`。",
        f"- 运行：`runtime/runs/{run_dir.name}/`。",
        f"- 报告更新：`{summary['reported_at']}`；阶段更新：`{state.get('updated_at', '')}`。",
        f"- 已记录任务执行：{summary['records']}；终点有效配对：{summary['paired_graded']}/{summary['expected_tasks']}。",
        f"- [运行报告](runtime/runs/{run_dir.name}/report/REPORT.md)。",
    ]
    if state.get("error"):
        latest.append(f"- 待解决：`{state['error']}`。")
    document.write_text(text + "\n".join(latest) + "\n")
