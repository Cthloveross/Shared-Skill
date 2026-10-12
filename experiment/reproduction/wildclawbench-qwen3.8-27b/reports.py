"""Reports from measured results only; incomplete stages retain their denominators."""

from __future__ import annotations

import csv
import datetime as dt
import json
import statistics
from collections import defaultdict
from pathlib import Path

from protocol import ROOT, tree_hash, write_json


def wall_span(records: list[dict]) -> float | None:
    if not records:
        return None
    starts = [dt.datetime.fromisoformat(r["started_at"]) for r in records if r.get("started_at")]
    ends = [dt.datetime.fromisoformat(r["finished_at"]) for r in records if r.get("finished_at")]
    return (max(ends) - min(starts)).total_seconds() if starts and ends else None


def summarize(records: list[dict]) -> dict:
    graded = [r for r in records if r.get("status") == "completed" and r.get("score") is not None]
    values = [r["score"] for r in graded]
    usage = [r["usage"] for r in records if isinstance(r.get("usage"), dict)]
    return {
        "attempted": len(records),
        "graded": len(graded),
        "score": statistics.mean(values) if values else None,
        "task_seconds": sum(r.get("wall_seconds", 0) for r in records),
        "timeouts": sum(r.get("timed_out", False) for r in records),
        "tool_errors": sum(r.get("tool_errors", 0) for r in records),
        "wall_span_seconds": wall_span(records),
        "usage_records": len(usage),
        "usage": {
            key: sum(u.get(key, 0) for u in usage) if usage else None
            for key in ("input_tokens", "output_tokens", "total_tokens", "request_count")
        },
    }


def generate(run_dir: Path, tasks: list[dict]) -> dict:
    output = run_dir / "report"
    output.mkdir(parents=True, exist_ok=True)
    records = []
    for path in sorted((run_dir / "executions").rglob("result.json")):
        result = json.loads(path.read_text())
        if result.get("experiment_record"):
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
    ]
    with (output / "tasks.csv").open("w") as stream:
        writer = csv.DictWriter(stream, fields, extrasaction="ignore")
        writer.writeheader()
        writer.writerows({**record, **record.get("usage", {})} for record in records)
    groups = defaultdict(list)
    for record in records:
        if record["phase"] in {"day", "endpoint", "pilot"}:
            groups[(record["phase"], record["round"], record.get("arm", ""))].append(record)
    stages = []
    for (phase, number, arm), rows in sorted(groups.items()):
        total = summarize(rows)
        by_category = {}
        for category in sorted({t["category"] for t in tasks}):
            category_rows = [r for r in rows if r["category"] == category]
            by_category[category] = {
                **summarize(category_rows),
                "expected": sum(t["category"] == category for t in tasks),
            }
        stages.append({"phase": phase, "round": number, "arm": arm, **total, "categories": by_category})
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
    decisions = [json.loads(path.read_text()) for path in sorted((run_dir / "nights").rglob("decision.json"))]
    valid_pairs = [p for p in paired if p["delta"] is not None]
    unchanged = [p for p in valid_pairs if not p["local_variant"]]
    state = json.loads((run_dir / "state.json").read_text()) if (run_dir / "state.json").exists() else {}
    phase_totals = {
        phase: summarize([r for r in records if r["phase"] == phase]) for phase in sorted({r["phase"] for r in records})
    }
    nights = []
    for path in sorted((run_dir / "nights").glob("*/publication.json")):
        publication = json.loads(path.read_text())
        proposal_path = path.parent / "proposals/proposal-summary.json"
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
    summary = {
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
    }
    write_json(output / "summary.json", summary)
    lines = [
        "# 实验运行结果",
        "",
        f"运行目录：`{run_dir.name}`；状态：**{state.get('status', 'not_started')}**。",
        "",
        "成绩由实际有效评分生成，缺失评分保留为空；不把部分任务均值当成完整 60 题成绩。",
        "",
        "| 阶段 | 库／轮次 | 已执行 | 有效评分 | 平均分 | 累计任务小时 | 超时 |",
        "|---|---|---:|---:|---:|---:|---:|",
    ]
    for stage in stages:
        score = f"{stage['score'] * 100:.2f}%" if stage["score"] is not None else "待评分"
        lines.append(
            f"| {stage['phase']} | {stage['arm'] or stage['round']} | {stage['attempted']} | "
            f"{stage['graded']} | {score} | {stage['task_seconds'] / 3600:.2f} | {stage['timeouts']} |"
        )
    lines += [
        "",
        f"候选接受 {summary['candidate_accepts']} 个，拒绝 {summary['candidate_rejects']} 个。",
        f"终点配对有效评分 {len(valid_pairs)}/60；其中保留原题提示 {len(unchanged)}/59。",
        "",
    ]
    if len(valid_pairs) == 60:
        lines.append(
            f"终点 S6−S0：{summary['endpoint_delta'] * 100:+.2f} 个百分点；"
            f"59 道保留原提示题：{summary['unchanged_prompt_delta'] * 100:+.2f} 个百分点。"
        )
    else:
        lines.append("终点对照尚未完成，不能给出最终技能进步幅度。")
    lines += [
        "",
        "| 工作类型 | 执行次数 | 累计任务小时 | 阶段墙钟小时 | Agent token 总量 |",
        "|---|---:|---:|---:|---:|",
    ]
    for phase, totals in phase_totals.items():
        span = f"{totals['wall_span_seconds'] / 3600:.2f}" if totals["wall_span_seconds"] is not None else ""
        tokens = totals["usage"]["total_tokens"]
        lines.append(f"| {phase} | {totals['attempted']} | {totals['task_seconds'] / 3600:.2f} | {span} | {tokens} |")
    lines += [
        "",
        "Agent token 来自实际 OpenClaw 轨迹；题目内部 OCR 子调用、评分和摘要另保存在 API 请求证据中。",
        "夜间摘要／提案耗时和接受技能见 summary.json 的 nights；版本 hash 与父版本另见 skill-lineage.csv。",
        "OCR 变体的任务完成分与模型 OCR 准确率分别记录，不能混为同一个分数。",
        "",
        "[逐题记录](tasks.csv) · [终点配对](endpoint-paired.csv) · [技能版本](skill-lineage.csv) · "
        "[OCR 变体](ocr-variant.json) · [机器可读汇总](summary.json)",
    ]
    # Standalone figure, not a Mermaid chart or screenshot.
    day_stages = sorted((s for s in stages if s["phase"] == "day" and s["graded"] == 60), key=lambda s: s["round"])
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
            axis.set_ylabel("Score (%)")
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
    document = ROOT / "EXPERIMENT.md"
    if not document.exists():
        return
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
        f"- 更新：`{state.get('updated_at', '')}`。",
        f"- 已记录任务执行：{summary['records']}；终点有效配对：{summary['paired_graded']}/60。",
        f"- [运行报告](runtime/runs/{run_dir.name}/report/REPORT.md)。",
    ]
    if state.get("error"):
        latest.append(f"- 待解决：`{state['error']}`。")
    document.write_text(text + "\n".join(latest) + "\n")
