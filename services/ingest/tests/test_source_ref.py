"""Tests for citation-revision resolution in the ingest entry point."""

import pytest

from ingest.__main__ import resolve_source_ref

SHA = "0123456789abcdef0123456789abcdef01234567"


def test_an_explicit_ref_wins_over_the_environment():
    assert (
        resolve_source_ref(SHA, env={"GITHUB_SHA": "f" * 40}, head_sha=lambda: "0" * 40)
        == SHA
    )


def test_github_sha_from_the_environment_is_used():
    assert resolve_source_ref(env={"GITHUB_SHA": SHA}, head_sha=lambda: "0" * 40) == SHA


def test_the_head_sha_fallback_is_used_without_env_or_explicit():
    assert resolve_source_ref(env={}, head_sha=lambda: SHA) == SHA


def test_an_explicit_branch_name_raises():
    with pytest.raises(ValueError, match="mutable ref"):
        resolve_source_ref("main", env={"GITHUB_SHA": SHA}, head_sha=lambda: SHA)


def test_a_non_hex_environment_value_raises():
    with pytest.raises(ValueError, match="full 40-hex"):
        resolve_source_ref(env={"GITHUB_SHA": "release"}, head_sha=lambda: SHA)


def test_a_failing_head_sha_lookup_propagates():
    def broken():
        raise ValueError("cannot resolve the citation revision")

    with pytest.raises(ValueError, match="cannot resolve"):
        resolve_source_ref(env={}, head_sha=broken)
