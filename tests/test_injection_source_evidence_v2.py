import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scripts.injection import source_evidence_v2 as sources


def history(tmp_path, name, text, version=1):
    root = tmp_path / name
    path = root / "history" / f"v{version}_evidence.md"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)
    return {"name": name, "bundle_dir": str(root)}, path


def day(source="day-one", normalized="native-one", task="02_Code_Intelligence_task_7_connect_the_dots_medium_img_zh"):
    return {"source_session_id": source, "session_id": normalized, "task_id": task}


def test_exact_session_resolves_abbreviated_label_without_guessing(tmp_path):
    session = day()
    label = "02_Code_Intelligence_task_7_connect_the_dots"
    bundle, path = history(tmp_path, "skill", f"Session `{session['session_id']}` ({label})")
    before = path.read_bytes()
    selected, evidence = sources.resolve_history_sources([bundle], {}, [session])
    assert selected == [session]
    assert evidence[0]["source_task_ids"] == [session["task_id"]]
    assert evidence[0]["diagnostics"]["unknown_task_labels"] == [label]
    assert evidence[0]["source_policy"] == sources.POLICY
    assert path.read_bytes() == before


def test_exact_task_and_session_sources_form_union(tmp_path):
    a, b = day(), day("day-two", "native-two", "03_Social_Interaction_task_1_meeting_negotiation")
    bundle, _ = history(tmp_path, "skill", f"`{a['source_session_id']}` and `{b['task_id']}`")
    selected, _ = sources.resolve_history_sources([bundle], {}, [a, b])
    assert selected == [a, b]


def test_abbreviation_alone_does_not_infer_matching_task(tmp_path):
    bundle, _ = history(tmp_path, "skill", "02_Code_Intelligence_task_7_connect_the_dots")
    with pytest.raises(ValueError, match="no changed native history with real day IDs"):
        sources.resolve_history_sources([bundle], {}, [day()])


def test_partial_session_and_old_history_cannot_supply_sources(tmp_path):
    session = day()
    bundle, path = history(tmp_path, "skill", "native-one-suffix")
    with pytest.raises(ValueError, match="no changed native history"):
        sources.resolve_history_sources([bundle], {}, [session])
    path.write_text(session["session_id"])
    bank = {"skill": {"history/v1_evidence.md": path.read_bytes()}}
    with pytest.raises(ValueError, match="no changed native history"):
        sources.resolve_history_sources([bundle], bank, [session])


@pytest.mark.parametrize("sessions", [
    [day(), day("day-other", "native-one", "other-task")],
    [day(), day("day-one", "native-other", "other-task")],
    [day(), day("native-one", "native-other", "other-task")],
])
def test_conflicting_session_bindings_fail(tmp_path, sessions):
    bundle, _ = history(tmp_path, "skill", "native-one")
    with pytest.raises(ValueError, match="ambiguous"):
        sources.resolve_history_sources([bundle], {}, sessions)


def test_unknown_session_identity_remains_a_failure(tmp_path):
    bundle, _ = history(tmp_path, "skill", "native-one and `0123456789abcdef01234567`")
    with pytest.raises(ValueError, match="non-day session identities"):
        sources.resolve_history_sources([bundle], {}, [day()])


def test_each_bundle_needs_a_verifiable_source(tmp_path):
    valid, _ = history(tmp_path, "valid", "native-one")
    invalid, _ = history(tmp_path, "invalid", "No exact IDs")
    with pytest.raises(ValueError, match="invalid has no changed native history"):
        sources.resolve_history_sources([valid, invalid], {}, [day()])


def test_scoped_hook_replaces_both_aliases_and_restores_outside_calls(tmp_path):
    calls = []

    def original(*args):
        calls.append(args)
        return "original"

    evolution = SimpleNamespace(_history_sources=original)
    controller = SimpleNamespace(_history_sources=original)
    scope = tmp_path / "versioned"
    scope.mkdir()
    inside, _ = history(scope, "skill", "native-one")
    outside, _ = history(tmp_path, "old", "native-one")
    with sources.scoped_history_sources(evolution, controller, scope):
        assert controller._history_sources is evolution._history_sources
        assert evolution._history_sources([inside], {}, [day()])[0] == [day()]
        assert evolution._history_sources([outside], {}, [day()]) == "original"
        with pytest.raises(ValueError, match="crosses"):
            evolution._history_sources([inside, outside], {}, [day()])
    assert evolution._history_sources is original and controller._history_sources is original
    assert len(calls) == 1


def test_scoped_hook_revalidates_only_explicitly_imported_original_bundles(tmp_path):
    native = lambda *args: "original"
    engine = SimpleNamespace(_history_sources=native)
    scope = tmp_path / "new-version"
    scope.mkdir()
    imported, path = history(tmp_path, "preserved-old-candidate", "native-one")
    original = path.read_bytes()
    with sources.scoped_history_sources(engine, None, scope, imported_bundle_dirs=[path.parent.parent]):
        assert engine._history_sources([imported], {}, [day()])[0] == [day()]
    assert engine._history_sources is native
    assert path.read_bytes() == original


def test_real_quarantined_nine_bundles_resolve_sixteen_tasks_without_writes():
    root = Path(__file__).resolve().parents[1]
    attempt = root / ("experiment/reproduction/dymalskill-trajectory-e2e/runtime/runs/"
        "30x5-sixcycles-20261016-01/rounds/01/clean/branches/clean/nights/01/proposals/evolution/attempt-920500f2de35")
    if not attempt.is_dir():
        pytest.skip("Preserved clean Night1 candidate fixture is unavailable")
    summary_path = attempt.parent / "proposal-summary.json"
    summary_bytes = summary_path.read_bytes()
    candidate = json.loads(summary_bytes)["quarantines"][0]
    bundles = candidate["bundles"]
    sessions = [json.loads(path.read_text()) for path in sorted((attempt / "native-summaries").glob("*.json"))]
    bank = {}
    files = {path: hashlib.sha256(path.read_bytes()).hexdigest() for bundle in bundles
             for path in Path(bundle["bundle_dir"]).rglob("*") if path.is_file()}
    from scripts.injection import e2e

    native = e2e.load_components()[3]
    with pytest.raises(ValueError, match="openrouter-local-api references non-day identities"):
        native._history_sources(bundles, bank, sessions)
    selected, evidence = sources.resolve_history_sources(bundles, bank, sessions)
    assert len(bundles) == 9
    assert len({session["task_id"] for session in selected}) == 16
    assert {row["bundle"] for row in evidence} == {bundle["name"] for bundle in bundles}
    unknown = [label for row in evidence for label in row["diagnostics"]["unknown_task_labels"]]
    assert unknown == ["02_Code_Intelligence_task_7_connect_the_dots"]
    assert summary_path.read_bytes() == summary_bytes
    assert all(hashlib.sha256(path.read_bytes()).hexdigest() == digest for path, digest in files.items())
