"""Example JSON files in examples/ must stay valid against the models."""

from __future__ import annotations

from shorts_factory.models import Analysis, Candidate, MediaManifest, QCReport, Script, SearchScope

from .conftest import load_example


def test_examples_validate():
    Analysis.model_validate(load_example("analysis_metadata_only.example.json"))
    Analysis.model_validate(load_example("analysis_full_media.example.json"))
    Script.model_validate(load_example("script.example.json"))
    QCReport.model_validate(load_example("qc_report_failed.example.json")["qc"])
    MediaManifest.model_validate(load_example("reference_manifest.example.json"))
    d = load_example("discover_output.example.json")
    SearchScope.model_validate(d["scope"])
    for c in d["candidates"]:
        Candidate.model_validate(c)
    res = load_example("upload_request.example.json")["resource"]
    assert res["status"]["containsSyntheticMedia"] is True and res["status"]["privacyStatus"] == "private"
