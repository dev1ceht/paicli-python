from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from paicli.evaluation.context_variants import load_context_stress_profile


def test_load_context_stress_profile_validates_and_fingerprints(tmp_path: Path) -> None:
    data = {
        "schema_version": 1,
        "profile_id": "test-16k-v1",
        "input_budget_tokens": 16384,
        "output_reserve_tokens": 4096,
    }
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    profile = load_context_stress_profile(path)

    assert profile.profile_id == "test-16k-v1"
    assert profile.input_budget_tokens == 16384
    assert profile.output_reserve_tokens == 4096
    assert profile.fingerprint == hashlib.sha256(
        json.dumps(data, sort_keys=True, separators=(",", ":")).encode()
    ).hexdigest()


@pytest.mark.parametrize(
    "data",
    [
        [],
        {"schema_version": 1},
        {
            "schema_version": 1,
            "profile_id": "",
            "input_budget_tokens": 16384,
            "output_reserve_tokens": 4096,
        },
        {
            "schema_version": 1,
            "profile_id": "invalid",
            "input_budget_tokens": True,
            "output_reserve_tokens": 4096,
        },
    ],
)
def test_load_context_stress_profile_rejects_invalid_data(
    tmp_path: Path, data: object
) -> None:
    path = tmp_path / "profile.json"
    path.write_text(json.dumps(data), encoding="utf-8")

    with pytest.raises(ValueError):
        load_context_stress_profile(path)
