"""Versioned illustrative rules, not a platform policy or compliance certification."""

from .store import utc_now
from .transcription import empty_transcript


PROFILE_VERSION = "illustrative-portrait-v1"
RULES_VERSION = "1.0.0"
PLACEMENT_PROFILE = {
    "id": PROFILE_VERSION,
    "officially_verified": False,
    "description": "Illustrative portrait placement; not an official social-platform specification.",
    "aspect_ratio": 9 / 16,
    "aspect_tolerance": 0.025,
    "minimum_width": 720,
    "minimum_height": 1280,
    "minimum_duration_seconds": 3,
    "maximum_duration_seconds": 60,
    "dark_pixel_fraction_review_threshold": 0.98,
}
SEMANTIC_CHECKS = (
    ("frame-safety", "Frame content safety"),
    ("transcript-safety", "Transcript content safety"),
    ("campaign-metadata", "AI-generated campaign metadata"),
    ("required-text", "Required text / OCR"),
)


def finding(rule_id, state, conclusion, evidence_ids=()):
    return {
        "rule_id": rule_id, "state": state, "conclusion": conclusion,
        "evidence_ids": list(evidence_ids),
    }


def evaluate(metadata, frames, required_text, *, error_code=None):
    """Raw extractor observations are separate from rule conclusions."""
    evidence = []
    findings = []
    if metadata:
        evidence.append({"id": "media-metadata", "kind": "ffprobe", "observations": metadata})
        width, height = metadata["width"], metadata["height"]
        findings.extend([
            finding("portrait-aspect", "pass" if abs(width / height - PLACEMENT_PROFILE["aspect_ratio"]) <= PLACEMENT_PROFILE["aspect_tolerance"] else "fail",
                    f"Illustrative aspect ratio: {PLACEMENT_PROFILE['aspect_ratio']:.6f}; absolute tolerance {PLACEMENT_PROFILE['aspect_tolerance']}.",
                    ["media-metadata"]),
            finding("minimum-resolution", "pass" if width >= PLACEMENT_PROFILE["minimum_width"] and height >= PLACEMENT_PROFILE["minimum_height"] else "fail",
                    f"Illustrative minimum: {PLACEMENT_PROFILE['minimum_width']} x {PLACEMENT_PROFILE['minimum_height']} coded pixels.", ["media-metadata"]),
            finding("duration", "pass" if PLACEMENT_PROFILE["minimum_duration_seconds"] <= metadata["duration_seconds"] <= PLACEMENT_PROFILE["maximum_duration_seconds"] else "fail",
                    f"Illustrative duration: {PLACEMENT_PROFILE['minimum_duration_seconds']}-{PLACEMENT_PROFILE['maximum_duration_seconds']} seconds.", ["media-metadata"]),
            finding("audio-present", "pass" if metadata["has_audio"] else "needs-review",
                    "Audio stream detected." if metadata["has_audio"] else "No audio stream detected.",
                    ["media-metadata"]),
            finding("rotation", "needs-review" if metadata.get("rotation_degrees", 0) else "pass",
                    "Display rotation requires manual review; aspect rules use coded dimensions."
                    if metadata.get("rotation_degrees", 0) else "No display rotation reported.",
                    ["media-metadata"]),
        ])
    else:
        findings.append(finding("technical-inspection", "not-evaluated",
                                "Media metadata could not be inspected."))
    if frames:
        evidence.append({"id": "sampled-frames", "kind": "ffmpeg-luminance", "observations": frames})
        dark = any(frame["dark_pixel_fraction"] >= PLACEMENT_PROFILE["dark_pixel_fraction_review_threshold"] for frame in frames)
        findings.append(finding(
            "sampled-dark-frames", "needs-review" if dark else "pass",
            "Near-black sampled frame detected; review manually." if dark else
            "No near-black frame among the sampled timestamps; not full-video coverage.",
            ["sampled-frames"],
        ))
    else:
        findings.append(finding("sampled-dark-frames", "not-evaluated", "No decoded frame samples."))
    for rule_id, name in SEMANTIC_CHECKS:
        findings.append(finding(
            rule_id, "not-evaluated",
            f"{name} provider is unconfigured; no claim of coverage or safety is made.",
        ))
    if error_code:
        findings.append(finding("processing-error", "needs-review",
                                f"Processing did not finish: {error_code}."))
    states = {item["state"] for item in findings}
    complete = not error_code and "not-evaluated" not in states
    overall = "fail" if "fail" in states else (
        "needs-review" if "needs-review" in states else (
            "not-evaluated" if "not-evaluated" in states else "pass"
        )
    )
    return {
        "report_version": "1.1",
        "profile": PROFILE_VERSION,
        "profile_definition": dict(PLACEMENT_PROFILE),
        "rules_version": RULES_VERSION,
        "generated_at": utc_now(),
        "disclaimer": "Illustrative local checks only; not platform approval or compliance certification.",
        "required_text": required_text,
        "transcript": empty_transcript(),
        "evidence": evidence,
        "findings": findings,
        "coverage": {
            "sampling_only": True,
            "sampled_frame_count": len(frames),
            "full_video_checked": False,
            "ocr_configured": False,
            "ai_verified": False,
        },
        "complete": complete,
        "ready": complete and overall == "pass",
        "overall_state": overall,
        "error_code": error_code,
    }
