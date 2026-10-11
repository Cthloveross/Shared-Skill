# Benign sharedskill workflow validation

This acceptance run exercises sharedskill's normal HTTP workflow with actual
XLSX files and model-generated tool calls. sharedskill is a renamed fork of
SkillClaw; historical results retain their original project names and metadata.

Two generated workbooks, Sales and Expenses, receive the requested Total
formulas while preserving the input files, original cells, sheets, and Arial
formatting. The real acceptance validator reopens the saved outputs and checks
the formulas and preservation rules. Its downstream viewer requires the header
row to remain visible when scrolling (`freeze_panes=A2`); this requirement is
reported through actual validator feedback when missing.

The client records and uploads the completed sessions to isolated local shared
storage. The workflow evolver runs its summarizer, session judge, skill decision,
skill verifier, direct publication, and version registry. The client reloads the
published skill, and the third workbook task, Inventory, must read that exact
version and pass acceptance. PRM scoring is disabled for this run.

## Replay

From the repository root, install the server and workbook-validation extras:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[server,validation]'
.venv/bin/python benign_validation/run_benign_e2e.py \
  --model gpt-5.6-sol \
  --output benign_validation/results/sharedskill-benign-replay
```

Use an authenticated `codex` CLI available on `PATH` and a model accessible to
that account. The output directory must be new or empty. The runner writes its
configuration there using `SHAREDSKILL_CONFIG_FILE`, starts three temporary
loopback services, and stops them when the run finishes. It preserves existing
result directories and does not configure a global agent installation.

The local [Codex bridge](codex_bridge.py) packages message roles as text in one
CLI prompt. It returns declared tool calls for the runner to execute and rejects
calls where the CLI invokes its own native tools. This adapter is not equivalent
to native Chat Completions role handling or sampling parameters. The run checks
the workflow, rather than paper accuracy or an attack success rate.

## Evidence

Each output directory contains:

- `run.json` with the outcome, enabled stages, version change, and reuse check.
- `workbooks/` with three actual input and completed XLSX files.
- `tasks/` with model requests, responses, executed tool results, and acceptance
  checks; `uploaded-sessions/` preserves the client-generated session records.
- `model-calls/`, service logs, and `native-debug/` for inspecting bridge calls
  and the evolver's prompts and output.
- `initial-SKILL.md`, `published-SKILL.md`, before/after manifests, the evolution
  cycle, and shared-storage versions for checking publication and reload.

A passing run requires valid workbooks, real skill-read attribution, successful
judge and verifier results, an advanced published version, and a subsequent
task that reads the published skill's exact hash. A legitimate `skip` decision
does not satisfy this particular publication acceptance check and is reported
as a failed run with its evidence retained.

The [synthetic poisoning harness](../reproduction/README.md) has a different,
limited scope: it exercises the execution-stage artifact mechanism without
performing workbook tasks or the full publication loop. Existing evidence under
`results/` predating the rename remains unchanged.
