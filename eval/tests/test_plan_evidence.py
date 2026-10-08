"""Hermetic plan freshness and approval-reuse controls with check-removal flips."""

import hashlib
import importlib.util
import json
import re
import subprocess
import sys
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[2]
HELPER = ROOT / "scripts/plan-evidence.py"
spec = importlib.util.spec_from_file_location("plan_evidence", HELPER)
assert spec is not None and spec.loader is not None
plan_evidence = importlib.util.module_from_spec(spec)
spec.loader.exec_module(plan_evidence)


def guard_pattern(guard):
    """Compile a whitespace-tolerant matcher for one helper guard source line."""
    escaped = re.escape(guard)
    escaped = escaped.replace("\\ ", r"\s+")
    escaped = escaped.replace("\\(", r"\(\s*")
    escaped = escaped.replace("\\)", r"\s*\)")
    return re.compile(escaped)


@pytest.mark.parametrize(
    "control,guard",
    [
        (
            "expired",
            "if not created <= now < expires or expires - created != timedelta(seconds=PLAN_TTL_SECONDS):",
        ),
        ("candidate", 'if metadata.get("candidate_sha256") != candidate:'),
        ("source", 'if metadata.get("source_commit") != source:'),
        ("run", 'if metadata.get("run_id") != run_id:'),
        ("attempt", 'if metadata.get("run_attempt") != attempt:'),
        ("hash", 'if metadata.get("plan_sha256") != digest:'),
        ("approval-reuse", "if metadata != approved:"),
    ],
)
def test_plan_controls_and_flips(tmp_path, control, guard):
    source, candidate = "a" * 40, "b" * 64
    plan = tmp_path / "tfplan"
    plan.write_bytes(b"canned private plan")
    metadata_path = tmp_path / "metadata.json"
    approved_path = tmp_path / "approved.json"
    args = [
        "--source",
        source,
        "--run-id",
        "123",
        "--attempt",
        "1",
        "--candidate",
        candidate,
        "--plan",
        str(plan),
        "--metadata",
        str(metadata_path),
    ]
    created = subprocess.run(
        [sys.executable, str(HELPER), "create", *args], capture_output=True, check=False
    )
    assert created.returncode == 0
    metadata = json.loads(metadata_path.read_text())
    original = dict(metadata)
    assert metadata["plan_sha256"] == hashlib.sha256(plan.read_bytes()).hexdigest()
    approved_path.write_text(json.dumps({"infrastructure_plan": metadata}))
    valid = subprocess.run(
        [
            sys.executable,
            str(HELPER),
            "verify",
            *args,
            "--approved",
            str(approved_path),
        ],
        capture_output=True,
        check=False,
    )
    assert valid.returncode == 0, valid.stderr
    if control == "expired":
        metadata["created_at"] = (
            datetime.now(UTC) - timedelta(seconds=8000)
        ).isoformat()
        metadata["expires_at"] = (
            datetime.now(UTC) - timedelta(seconds=800)
        ).isoformat()
    elif control == "candidate":
        metadata["candidate_sha256"] = "c" * 64
    elif control == "source":
        metadata["source_commit"] = "c" * 40
    elif control == "run":
        metadata["run_id"] = "124"
    elif control == "attempt":
        metadata["run_attempt"] = "2"
    elif control == "hash":
        metadata["plan_sha256"] = "c" * 64
    else:
        args[args.index("--attempt") + 1] = "2"
        args[args.index("--candidate") + 1] = plan_evidence.candidate_hash(
            source, "123", "2", ["infra"]
        )
        regenerated = subprocess.run(
            [sys.executable, str(HELPER), "create", *args],
            capture_output=True,
            check=False,
        )
        assert regenerated.returncode == 0
        metadata = json.loads(metadata_path.read_text())
        assert metadata != original
    metadata_path.write_text(json.dumps(metadata))
    approved_path.write_text(
        json.dumps(
            {
                "infrastructure_plan": original
                if control == "approval-reuse"
                else metadata
            }
        )
    )
    command = ["verify", *args, "--approved", str(approved_path)]
    result = subprocess.run(
        [sys.executable, str(HELPER), *command], capture_output=True, check=False
    )
    assert result.returncode == 1, (control, result.stderr)
    text = HELPER.read_text()
    pattern = guard_pattern(guard)
    matches = pattern.findall(text)
    assert len(matches) == 1, (control, guard, len(matches))
    text = pattern.sub("if False:", text, count=1)
    text = text.replace(
        'str(Path(__file__).resolve().parents[1] / "eval")', repr(str(ROOT / "eval"))
    )
    mutated = tmp_path / "mutated.py"
    mutated.write_text(text)
    flipped = subprocess.run(
        [sys.executable, str(mutated), *command], capture_output=True, check=False
    )
    assert flipped.returncode == 0, (control, flipped.stderr)
    print(
        f"plan control {control}: expected=1 actual={result.returncode}; removed-check flip={flipped.returncode}"
    )


def test_ttl_guard_matcher_spans_formatter_wrapping():
    single_line = "if not created <= now < expires or expires - created != timedelta(seconds=PLAN_TTL_SECONDS):"
    wrapped = (
        "if not created <= now < expires or expires - created != timedelta(\n"
        "    seconds=PLAN_TTL_SECONDS\n"
        "):"
    )
    pattern = guard_pattern(single_line)
    assert len(pattern.findall(single_line)) == 1
    assert len(pattern.findall(wrapped)) == 1
    assert len(pattern.findall(HELPER.read_text())) == 1
    assert HELPER.read_bytes().count(single_line.encode()) == 0


def test_candidate_hash_selection_and_attempt_binding():
    first = plan_evidence.candidate_hash("a" * 40, "123", "1", ["kb", "app"])
    assert first == plan_evidence.candidate_hash("a" * 40, "123", "1", ["app", "kb"])
    assert first != plan_evidence.candidate_hash("a" * 40, "123", "2", ["app", "kb"])
    assert first != plan_evidence.candidate_hash("a" * 40, "123", "1", ["infra"])


@pytest.mark.parametrize(
    "components",
    [
        ["app", "app"],
        ["app", "kb", "app"],
        ["bogus"],
        ["Power_Platform"],
        ["app", ""],
    ],
)
def test_candidate_mode_rejects_unknown_and_duplicate_components(components):
    command = [
        sys.executable,
        str(HELPER),
        "candidate",
        "--source",
        "a" * 40,
        "--run-id",
        "123",
        "--attempt",
        "1",
        "--components",
        *components,
    ]
    result = subprocess.run(command, capture_output=True, check=False)
    assert result.returncode == 1, (components, result.stdout)
    assert result.stderr.startswith(b"plan gate: "), (components, result.stderr)
    assert plan_evidence.canonical_components([]) == []
