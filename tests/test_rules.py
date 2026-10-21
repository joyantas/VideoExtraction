from videoextration.rules import SEMANTIC_CHECKS, evaluate


def metadata(**changes):
    return {
        "width": 1080, "height": 1920, "duration_seconds": 10, "fps": 30,
        "has_audio": True, "rotation_degrees": 0, **changes,
    }


def samples(dark=0.01):
    return [{"index": 0, "requested_timestamp_seconds": 5, "mean_luminance": 110, "dark_pixel_fraction": dark}]


def test_technical_pass_never_claims_unconfigured_ai_or_ocr_pass():
    report = evaluate(metadata(), samples(), ["USER SUPPLIED"])
    assert report["ready"] is False and report["complete"] is False
    assert report["overall_state"] == "not-evaluated"
    assert report["required_text"] == ["USER SUPPLIED"]
    assert report["coverage"]["ai_verified"] is False
    assert report["coverage"]["ocr_configured"] is False
    by_id = {finding["rule_id"]: finding for finding in report["findings"]}
    for rule_id, _ in SEMANTIC_CHECKS:
        assert by_id[rule_id]["state"] == "not-evaluated"
    assert by_id["portrait-aspect"]["state"] == "pass"
    assert report["generated_at"].endswith("Z")
    assert report["rules_version"] == "1.0.0"
    assert report["profile"] == "illustrative-portrait-v1"
    evidence_ids = {item["id"] for item in report["evidence"]}
    assert all(set(item["evidence_ids"]).issubset(evidence_ids) for item in report["findings"])


def test_fail_and_review_states():
    report = evaluate(metadata(width=1920, height=1080), samples(), [])
    assert report["overall_state"] == "fail" and report["ready"] is False
    report = evaluate(metadata(has_audio=False), samples(dark=1), [])
    assert report["overall_state"] == "needs-review"
    assert next(f for f in report["findings"] if f["rule_id"] == "sampled-dark-frames")["state"] == "needs-review"


def test_errors_preserve_unknowns_not_green():
    report = evaluate(None, [], [], error_code="invalid-or-unsupported-media")
    assert report["overall_state"] == "needs-review"
    assert report["complete"] is False and report["ready"] is False
    assert report["evidence"] == []
    assert report["coverage"]["sampled_frame_count"] == 0
    assert all(item["state"] != "pass" for item in report["findings"])
