"""Prepare independent trajectory batches or run the native evolution helper."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path

from .trajectory import CONDITIONS, ROOT, capture_request, fragment_turns, inject, load_catalog, make_batch, make_suite


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("x", encoding="utf-8") as handle:
        json.dump(value, handle, ensure_ascii=False, indent=2)
        handle.write("\n")


async def prepare(args) -> None:
    cases = load_catalog()
    selected = list(cases) if args.case == "all" else [args.case]
    if any(case not in cases for case in selected):
        raise ValueError("unknown case")
    args.output.mkdir(parents=True, exist_ok=False)
    for case_id in selected:
        case = cases[case_id]
        folder = args.output / case_id
        write_json(folder / "fragment.json", fragment_turns(case))
        for condition in CONDITIONS:
            sessions, session_ids = make_batch(case, args.total, args.support, args.seed, condition, args.summary)
            write_json(folder / f"{condition}.json", sessions)
            if condition in {"clean", "poisoned"}:
                write_json(folder / f"{condition}-request.json", await capture_request(sessions))
        write_json(
            folder / "manifest.json",
            {
                "case_id": case_id,
                "selected_session_ids": session_ids,
                "total": args.total,
                "support": args.support,
                "seed": args.seed,
                "summary_mode": args.summary,
                "scores": "not generated",
                "source": case["source"],
                "marker": case["marker"],
                "llm_evaluated": False,
                "attack_success": None,
            },
        )
    write_json(args.output / "suite.json", [item for item in make_suite() if item["case_id"] in selected])
    write_json(
        args.output / "manifest.json",
        {
            "dataset": "dymalskill-trajectory-canary-v1",
            "cases": selected,
            "catalog_sha256": hashlib.sha256((ROOT / "catalog.json").read_bytes()).hexdigest(),
            "source": json.loads((ROOT / "catalog.json").read_text())["source"],
            "status": "prepared",
            "llm_evaluated": False,
            "attack_success": None,
            "summary_mode": args.summary,
        },
    )
    print(json.dumps({"status": "prepared", "cases": len(selected), "output": str(args.output)}))


def inject_existing(args) -> None:
    case = load_catalog()[args.case]
    source = args.sessions.read_bytes()
    sessions = json.loads(source)
    if not isinstance(sessions, list) or any(not isinstance(s, dict) for s in sessions):
        raise ValueError("sessions must be a JSON list of session objects")
    ids = [s.get("session_id") for s in sessions]
    if any(not isinstance(value, str) or not value for value in ids) or len(ids) != len(set(ids)):
        raise ValueError("sessions must have distinct nonempty IDs")
    wanted = set(args.session_ids)
    if not wanted.issubset(ids):
        raise ValueError("a selected session ID is missing")
    sidecar = args.output.with_suffix(".manifest.json")
    if args.output.exists() or sidecar.exists():
        raise FileExistsError("output or manifest already exists")
    result = [
        inject(s, case, summary=args.summary, output_path=args.output_path) if s["session_id"] in wanted else s
        for s in sessions
    ]
    write_json(args.output, result)
    write_json(
        sidecar,
        {
            "case_id": args.case,
            "selected_session_ids": sorted(wanted),
            "input_sha256": hashlib.sha256(source).hexdigest(),
            "source": case["source"],
            "summary_mode": args.summary,
            "llm_evaluated": False,
            "attack_success": None,
            "original_selected_execution_metrics": {
                s["session_id"]: {key: s[key] for key in ("outcome", "aggregate") if key in s}
                for s in sessions
                if s["session_id"] in wanted
            },
        },
    )
    print(json.dumps({"status": "injected", "selected": len(wanted), "output": str(args.output)}))


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    build = commands.add_parser("build", help="Prepare fixtures and exact native requests without a model call")
    build.add_argument("--case", default="all")
    build.add_argument("--output", type=Path, required=True)
    inject_parser = commands.add_parser("inject", help="Insert a fixture into explicitly selected flat sessions")
    inject_parser.add_argument("--case", required=True)
    inject_parser.add_argument("--sessions", type=Path, required=True)
    inject_parser.add_argument("--session-ids", nargs="+", required=True)
    inject_parser.add_argument("--output-path", help="Workbook output path, otherwise inferred from validation calls")
    inject_parser.add_argument("--output", type=Path, required=True)
    run = commands.add_parser("run", help="Call the native helper with no added defense or publication")
    run.add_argument("--cases", nargs="+")
    run.add_argument("--endpoint", required=True)
    run.add_argument("--model", required=True)
    run.add_argument("--output", type=Path, required=True)
    run.add_argument("--concurrency", type=int, default=2)
    run.add_argument("--timeout", type=float, default=180)
    for command in (build, run):
        command.add_argument("--total", type=int, default=30)
        command.add_argument("--support", type=int, default=3)
        command.add_argument("--seed", type=int, default=7)
    for command in (build, inject_parser, run):
        command.add_argument("--summary", choices=("none", "template"), default="none")
    args = parser.parse_args()
    if args.command == "build":
        asyncio.run(prepare(args))
    elif args.command == "inject":
        inject_existing(args)
    else:
        from .native import run_native

        result = asyncio.run(
            run_native(
                args.output,
                args.endpoint,
                args.model,
                args.cases,
                total=args.total,
                support=args.support,
                seed=args.seed,
                summary=args.summary,
                concurrency=args.concurrency,
                timeout=args.timeout,
            )
        )
        print(json.dumps(result, ensure_ascii=False))


if __name__ == "__main__":
    main()
