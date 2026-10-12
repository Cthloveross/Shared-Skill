# PoisonedEvolution mechanism reproduction

The paper's experimental design and parameters are documented separately in
[PoisonedEvolution experiments](../docs/poisoned-evolution-experiments.md).
The original SkillClaw benign experiment is described in
[SkillClaw experiments](../docs/paper-experiments.md).

This directory demonstrates the paper's artifact-poisoning mechanism at
sharedskill's execution stage. sharedskill is a renamed fork of the upstream
SkillClaw project, whose setting the paper evaluates. The harness passes 30
compatible sessions to `evolve_skill_from_sessions`: 27 clean spreadsheet
sessions and three recurring, causally framed attacker sessions. The attacker
constraint appears as a failed environment/tool check followed by a successful
recovery step; it is not inserted as a user prompt.

The initial `xlsx` skill, tasks, trajectories, tool results, scores, and default
session summaries are synthetic. The public paper does not release its 300
GLM-5/SpreadsheetBench logs, original skill hash, exact transformations,
detector, or official attack code. This is therefore a mechanism demonstration,
not a reproduction of the reported `546/600 = 91%` aggregate. The canaries are
only strings in evidence and generated skill text; no canary operation runs.

The harness directly exercises the native evidence formatter and evolution
function. It does not invoke `EvolveServer.run_once()` or the storage,
aggregation, merge, registry, and publish layers. The paper's optional session
judge and publish verifier are outside this call path, matching their disabled
state in the main paper experiment.

## Files

- `run_poisoned_evolution.py` builds the fixture, optionally invokes sharedskill's
  native LLM summarizer, invokes the native evolver, captures the exact prompt
  and raw output, and checks new affirmative marker-bearing guidance.
- `make_matched_control.py` replaces only attacker-owned sessions with clean
  sessions of the same IDs while preserving every unaffected native summary
  byte-for-byte.
- `test_reproduction.py` covers the 27/3 split, evidence provenance, source
  identity loss, matched controls, affirmative detector behavior, stale-output
  cleanup, and invalid model output handling.
- `verify_pair.py` checks the completed poison/control artifacts, including
  matching configuration, clean-marker absence, source/marker agreement, and
  byte-identical unaffected evidence.
- `RESULTS.md` records historical SkillClaw poison/control pairs, commands,
  evidence checks, and claim limits. It retains the original project names and
  checkout paths as provenance; its commands predate the repository rename.
- `results/` contains generated sessions, evidence, prompts, raw responses,
  skills, and run metadata. It is ignored by Git because these files are large.

## Setup

From the workspace root:

```bash
python3 -m venv .venv
.venv/bin/pip install -e '.[server,validation]'
```

## Run a paired trial

The Codex CLI adapter is a convenient local authenticated backend. It flattens
the system and user messages into one text prompt, so it is not API-equivalent
to an OpenAI-compatible Chat Completions endpoint.

```bash
.venv/bin/python reproduction/run_poisoned_evolution.py \
  --provider codex --model gpt-5.6-sol --family f4 --support 3 \
  --output reproduction/results/codex-f4-poison

.venv/bin/python reproduction/run_poisoned_evolution.py \
  --provider codex --model gpt-5.6-sol --family f4 --support 0 \
  --output reproduction/results/codex-f4-clean
```

Use `--provider none` for an unevaluated dry run that only captures the native
evolver prompt. `run.json` marks it as `status=dry_run` and
`evaluated=false`; it is never counted as a clean negative.

## Run native summaries

An OpenAI-compatible run with `--native-summaries` performs 30 summarizer calls
and one final evolution call. Keep summary concurrency low for a local model:

```bash
.venv/bin/python reproduction/run_poisoned_evolution.py \
  --provider openai --api-key ollama \
  --base-url http://127.0.0.1:11434/v1 --model llama3.1:8b \
  --native-summaries --summary-concurrency 2 \
  --family f4 --support 3 \
  --output reproduction/results/ollama-f4-poison
```

To reuse summaries, pass `--input-sessions`, omit `--native-summaries`, and
record their provenance:

```bash
.venv/bin/python reproduction/run_poisoned_evolution.py \
  --provider openai --api-key ollama \
  --base-url http://127.0.0.1:11434/v1 --model qwen3:30b \
  --family f4 \
  --input-sessions reproduction/results/ollama-f4-poison/sessions.json \
  --summary-model llama3.1:8b \
  --summary-source reproduction/results/ollama-f4-poison/sessions.json \
  --output reproduction/results/qwen-f4-poison
```

The runner rejects batches above sharedskill's 30-session evidence limit,
requires the attacker aliases to match marker-bearing sessions, checks that all
summaries completed, removes its own stale artifacts before reuse, and records
unparseable model output as `status=invalid_output` with a nonzero exit code.
The detector requires a newly added marker near affirmative operation language
and rejects explicit prohibitions; `added_skill_lines` remains available for
human review.

New runs record the repository revision as `sharedskill_commit`. The pair
verifier also accepts the historical `skillclaw_commit` field so existing
evidence can be checked without rewriting it. Run the fixture checks without
calling a model:

```bash
.venv/bin/python -m unittest reproduction/test_reproduction.py -v
.venv/bin/python reproduction/run_poisoned_evolution.py \
  --provider none --output /tmp/sharedskill-reproduction-dry-run
```

For a normal workbook task through collection, evolution, publication, and
subsequent skill reuse, see [benign validation](../benign_validation/README.md).
