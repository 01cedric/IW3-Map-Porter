#!/usr/bin/env python3
from __future__ import annotations

import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from cod4porter.cli import MAP_NAME_RE, main


def run() -> dict[str, object]:
    accepted = ("mp_nuked", "mp_a", "mp_test_map_2")
    rejected = (
        "MP_NUKED",
        "mp_nuked (1)",
        "mp_nuked&whoami",
        "mp_nuked|whoami",
        "mp_nuked/../x",
        "sp_nuked",
        "mp_ü",
    )
    assert all(MAP_NAME_RE.fullmatch(value) for value in accepted)
    assert all(not MAP_NAME_RE.fullmatch(value) for value in rejected)
    # Validation happens before any source path is opened.
    assert main(["analyze", "--pc-ff", "does-not-exist.ff", "--map", rejected[2]]) == 2
    return {"passed": True, "accepted": len(accepted), "rejected": len(rejected)}


if __name__ == "__main__":
    print(run())
