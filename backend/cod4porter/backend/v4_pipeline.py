from __future__ import annotations

import argparse
from dataclasses import asdict
import json
from pathlib import Path
import traceback
from typing import Any

from .v4_ffio import (
    read_ps3_fastfile,
    read_ps3_fastfile_bytes,
    parse_xasset_list,
    summarize_fastfile,
    build_ps3_fastfile,
    sha256,
)
from .v4_load import parse_load_zone
from .v4_source import inspect_pc_source, audit_csharp_source
from .v4_gate import (
    evidence,
    evidence_from_pc_source,
    evidence_from_load_readback,
    evidence_from_conversion_report,
    evaluate_full_fidelity,
)

DEFAULT_PC_EXPECTED = {
    "script_strings": 469,
    "xassets": 767,
    "iwi": 735,
    "audio": 17,
}


def _read_json(path: str | Path | None) -> dict:
    if not path:
        return {}
    return json.loads(Path(path).read_text(encoding="utf-8-sig"))


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(obj, indent=2, ensure_ascii=False), encoding="utf-8")


def _load_external_evidence(paths: list[str]) -> list[dict]:
    docs: list[dict] = []
    for raw in paths:
        p = Path(raw)
        doc = _read_json(p)
        if isinstance(doc, dict) and ("evidence" in doc or "criterion" in doc):
            docs.append(doc)
        elif isinstance(doc, list):
            docs.append({"source_file": str(p), "evidence": doc})
        else:
            raise ValueError(f"external evidence file does not contain evidence records: {p}")
    return docs


def _ps3_output_readback(main_path: Path | None, load_path: Path | None) -> tuple[dict, list[dict]]:
    report: dict[str, Any] = {"main": None, "load": None, "errors": []}
    evidence_docs: list[dict] = []

    if main_path:
        try:
            main_doc = read_ps3_fastfile(main_path)
            main_assets = parse_xasset_list(main_doc.zone, "ps3")
            first = build_ps3_fastfile(main_doc.zone, main_doc.trailer)
            second = build_ps3_fastfile(main_doc.zone, main_doc.trailer)
            deterministic = first == second
            report["main"] = {
                "summary": summarize_fastfile(main_path, "ps3"),
                "xassets": len(main_assets.assets),
                "script_strings": len(main_assets.script_strings),
                "reencode_sha256": sha256(first),
                "deterministic_reencode": deterministic,
                "reencoded_zone_roundtrip": read_ps3_fastfile_bytes(first).zone == main_doc.zone,
            }
            evidence_docs.append({"source": "ps3_readback", "evidence": [
                evidence(
                    "deterministic_write",
                    deterministic,
                    "ps3_readback",
                    {
                        "first_sha256": sha256(first),
                        "second_sha256": sha256(second),
                        "zone_sha256": main_doc.zone_sha256,
                    },
                )
            ]})
        except Exception as exc:
            report["errors"].append({"part": "main", "error": str(exc), "trace": traceback.format_exc()})
            evidence_docs.append({"source": "ps3_readback", "evidence": [
                evidence("deterministic_write", False, "ps3_readback", {"error": str(exc)})
            ]})

    if load_path:
        try:
            load_doc = read_ps3_fastfile(load_path)
            profile = parse_load_zone(load_doc.zone)
            profile_dict = asdict(profile)
            load_re1 = build_ps3_fastfile(load_doc.zone, load_doc.trailer)
            load_re2 = build_ps3_fastfile(load_doc.zone, load_doc.trailer)
            load_deterministic = load_re1 == load_re2
            report["load"] = {
                "summary": summarize_fastfile(load_path, "ps3"),
                "profile": profile_dict,
                "deterministic_reencode": load_deterministic,
                "reencode_sha256": sha256(load_re1),
            }
            evidence_docs.append(evidence_from_load_readback(profile_dict, load_doc.file_sha256))
            if not main_path:
                evidence_docs.append({"source": "ps3_readback", "evidence": [
                    evidence("deterministic_write", load_deterministic, "ps3_readback", {
                        "artifact": "load",
                        "first_sha256": sha256(load_re1),
                        "second_sha256": sha256(load_re2),
                        "zone_sha256": load_doc.zone_sha256,
                    })
                ]})
        except Exception as exc:
            report["errors"].append({"part": "load", "error": str(exc), "trace": traceback.format_exc()})
            evidence_docs.append({"source": "ps3_readback", "evidence": [
                evidence("load_zone_exact", False, "ps3_readback", {"error": str(exc)})
            ]})

    return report, evidence_docs


def run_pipeline(config: dict) -> dict:
    output_dir = Path(config.get("output_dir") or "FIX114_v4_pipeline_out")
    output_dir.mkdir(parents=True, exist_ok=True)

    report: dict[str, Any] = {
        "revision": "FIX114-backend-v4",
        "backend_only": True,
        "config": config,
        "pc_source": None,
        "csharp_reference_audit": None,
        "conversion_report": None,
        "ps3_readback": None,
        "external_evidence_files": list(config.get("evidence_files") or []),
        "errors": [],
    }
    evidence_docs: list[dict] = []

    pc_ff = config.get("pc_ff")
    iwds = list(config.get("iwds") or [])
    expected = dict(DEFAULT_PC_EXPECTED)
    expected.update(config.get("pc_expected") or {})
    if pc_ff:
        try:
            pc = inspect_pc_source(pc_ff, iwds, expected, bool(config.get('deep_assets')), bool(config.get('deep_sound')))
            report["pc_source"] = pc
            evidence_docs.append(evidence_from_pc_source(pc, expected))
        except Exception as exc:
            report["errors"].append({"part": "pc_source", "error": str(exc), "trace": traceback.format_exc()})
            # Explicit negative evidence ensures a malformed/missing configured PC source cannot be masked.
            for crit in ("original_pc_ff_parsed", "iwd_inventory_exact", "source_xasset_profile_exact"):
                evidence_docs.append({"source": "pc_source_deep", "evidence": [
                    evidence(crit, False, "pc_source_deep", {"error": str(exc)})
                ]})

    source_root = config.get("source_root")
    if source_root:
        try:
            report["csharp_reference_audit"] = audit_csharp_source(source_root)
        except Exception as exc:
            report["errors"].append({"part": "source_root", "error": str(exc), "trace": traceback.format_exc()})

    conversion_path = config.get("conversion_report")
    if conversion_path:
        try:
            conversion = _read_json(conversion_path)
            report["conversion_report"] = conversion
            evidence_docs.append(evidence_from_conversion_report(conversion))
        except Exception as exc:
            report["errors"].append({"part": "conversion_report", "error": str(exc), "trace": traceback.format_exc()})

    main_path = Path(config["ps3_main"]) if config.get("ps3_main") else None
    load_path = Path(config["ps3_load"]) if config.get("ps3_load") else None
    if main_path or load_path:
        ps3_report, ps3_evidence = _ps3_output_readback(main_path, load_path)
        report["ps3_readback"] = ps3_report
        evidence_docs.extend(ps3_evidence)

    try:
        evidence_docs.extend(_load_external_evidence(list(config.get("evidence_files") or [])))
    except Exception as exc:
        report["errors"].append({"part": "evidence_files", "error": str(exc), "trace": traceback.format_exc()})

    gate = evaluate_full_fidelity(*evidence_docs)
    report["evidence_documents"] = evidence_docs
    report["full_fidelity_gate"] = gate
    report["backend_pipeline_passed"] = not report["errors"]
    report["full_fidelity_passed"] = bool(gate["full_fidelity_passed"] and not report["errors"])

    _write_json(output_dir / "FIX114_V4_PIPELINE_REPORT.json", report)
    _write_json(output_dir / "FIX114_V4_FULL_FIDELITY_GATE.json", gate)
    return report


def main() -> int:
    ap = argparse.ArgumentParser(description="FIX114 V4 one-pass fail-closed backend pipeline")
    ap.add_argument("config", help="JSON config")
    args = ap.parse_args()
    config = _read_json(args.config)
    result = run_pipeline(config)
    gate = result["full_fidelity_gate"]
    print(json.dumps({
        "backend_pipeline_passed": result["backend_pipeline_passed"],
        "full_fidelity_passed": result["full_fidelity_passed"],
        "passed_count": gate["passed_count"],
        "required_count": gate["required_count"],
        "failed": gate["failed"],
        "errors": [x["part"] for x in result["errors"]],
    }, indent=2))
    return 0 if result["full_fidelity_passed"] else 2


if __name__ == "__main__":
    raise SystemExit(main())
