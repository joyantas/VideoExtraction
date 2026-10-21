"""Opt-in Azure AI evidence collection. Generated fields are never proof of compliance."""

from __future__ import annotations

import base64
import json
import os
import time
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import httpx
from azure.core.exceptions import AzureError


CATEGORIES = {"Hate", "SelfHarm", "Sexual", "Violence"}


class ServiceError(RuntimeError):
    """A remote service failed or returned an unsupported/incomplete contract."""


@dataclass(frozen=True)
class AISettings:
    enabled: bool = False
    understanding_endpoint: str = ""
    analyzer_id: str = "campaign-video-v1"
    safety_endpoint: str = ""
    ocr_endpoint: str = ""
    deadline_seconds: int = 600
    max_frames: int = 12
    max_text_chars: int = 100_000
    model_deployments_json: str = "{}"

    @classmethod
    def from_env(cls) -> "AISettings":
        return cls(
            enabled=os.getenv("ENABLE_PAID_AI", "false").lower() == "true",
            understanding_endpoint=os.getenv("CU_ENDPOINT", ""),
            analyzer_id=os.getenv("CU_ANALYZER_ID", "campaign-video-v1"),
            safety_endpoint=os.getenv("SAFETY_ENDPOINT", ""),
            ocr_endpoint=os.getenv("OCR_ENDPOINT", ""),
            model_deployments_json=os.getenv("CU_MODEL_DEPLOYMENTS", "{}"),
        )


class AzureAI:
    def __init__(self, settings: AISettings, credential, client: httpx.Client | None = None):
        self.settings = settings
        self.credential = credential
        self.client = client or httpx.Client(timeout=30, follow_redirects=False)
        self.deadline = 0.0

    @staticmethod
    def endpoint(value: str) -> str:
        parsed = urlparse(value)
        if (
            parsed.scheme != "https"
            or not parsed.hostname
            or parsed.username
            or parsed.password
            or parsed.query
            or parsed.fragment
            or parsed.path not in ("", "/")
        ):
            raise ServiceError("AI endpoint must be an HTTPS resource origin, without credentials or path.")
        return value.rstrip("/")

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ServiceError("Azure AI total processing deadline exceeded.")
        try:
            token = self.credential.get_token("https://cognitiveservices.azure.com/.default").token
            remaining = self.deadline - time.monotonic()
            if remaining <= 0:
                raise ServiceError("Azure AI credential acquisition exceeded the processing deadline.")
            # Stream and cap the response before decoding; never log signed input URLs.
            with self.client.stream(
                method, url, headers={"Authorization": f"Bearer {token}"},
                timeout=min(30, remaining), **kwargs
            ) as response:
                if response.status_code not in (200, 201, 202):
                    raise ServiceError(f"Azure AI returned HTTP {response.status_code}.")
                body = bytearray()
                for part in response.iter_bytes():
                    body.extend(part)
                    if len(body) > 16 * 1024 * 1024:
                        raise ServiceError("Azure AI response exceeds the 16 MiB evidence limit.")
                return httpx.Response(
                    response.status_code, headers=response.headers, content=bytes(body),
                    request=response.request,
                )
        except httpx.HTTPError as exc:
            raise ServiceError(f"Azure AI transport failed: {type(exc).__name__}.") from exc
        except AzureError as exc:
            raise ServiceError(f"Azure AI credential acquisition failed: {type(exc).__name__}.") from exc

    @staticmethod
    def document(response: httpx.Response) -> dict:
        try:
            value = response.json()
        except ValueError as exc:
            raise ServiceError("Azure AI returned invalid JSON.") from exc
        if not isinstance(value, dict):
            raise ServiceError("Azure AI response must be an object.")
        return value

    def poll(self, response: httpx.Response, origin: str) -> dict:
        location = response.headers.get("Operation-Location")
        parsed = urlparse(location or "")
        if (
            not location
            or parsed.scheme != "https"
            or parsed.netloc != urlparse(origin).netloc
            or parsed.username
            or parsed.fragment
        ):
            raise ServiceError("Azure AI returned a missing or off-origin operation URL.")
        while time.monotonic() < self.deadline:
            result = self.document(self.request("GET", location))
            status = str(result.get("status", "")).lower()
            if status == "succeeded":
                return result
            if status not in ("running", "notstarted"):
                raise ServiceError(f"Azure AI operation did not succeed (status={status[:40]!r}).")
            time.sleep(min(2, max(0, self.deadline - time.monotonic())))
        raise ServiceError("Azure AI operation timed out.")

    def understanding(self, video_url: str) -> dict:
        endpoint = self.endpoint(self.settings.understanding_endpoint)
        analyzer = self.settings.analyzer_id
        if not analyzer or any(c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in analyzer):
            raise ServiceError("Invalid configured Content Understanding analyzer ID.")
        try:
            mappings = json.loads(self.settings.model_deployments_json)
        except ValueError as exc:
            raise ServiceError("CU_MODEL_DEPLOYMENTS must be a JSON object.") from exc
        if not isinstance(mappings, dict) or any(
            not isinstance(key, str) or not isinstance(value, str) or not value
            for key, value in mappings.items()
        ):
            raise ServiceError("CU_MODEL_DEPLOYMENTS must map supported model names to deployment names.")
        body = {"inputs": [{"url": video_url}]}
        if mappings:
            body["modelDeployments"] = mappings
        response = self.request(
            "POST",
            f"{endpoint}/contentunderstanding/analyzers/{analyzer}:analyze?api-version=2025-11-01",
            json=body,
        )
        result = self.poll(response, endpoint)
        analysis = result.get("result")
        contents = analysis.get("contents") if isinstance(analysis, dict) else None
        if not isinstance(contents, list) or not contents:
            raise ServiceError("Content Understanding returned no contents.")
        if any(not isinstance(content, dict) for content in contents):
            raise ServiceError("Content Understanding returned invalid content evidence.")
        return result

    def ocr(self, frame: Path) -> dict:
        endpoint = self.endpoint(self.settings.ocr_endpoint)
        response = self.request(
            "POST",
            f"{endpoint}/documentintelligence/documentModels/prebuilt-read:analyze?api-version=2024-11-30",
            json={"base64Source": base64.b64encode(frame.read_bytes()).decode("ascii")},
        )
        result = self.poll(response, endpoint)
        analysis = result.get("analyzeResult")
        if not isinstance(analysis, dict) or not isinstance(analysis.get("content"), str):
            raise ServiceError("Read OCR returned no content evidence.")
        return result

    def safety(self, *, frame: Path | None = None, text: str | None = None) -> dict:
        endpoint = self.endpoint(self.settings.safety_endpoint)
        if frame is not None:
            if frame.stat().st_size > 4 * 1024 * 1024:
                raise ServiceError("Sampled safety frame exceeds 4 MiB.")
            kind = "image"
            body = {"image": {"content": base64.b64encode(frame.read_bytes()).decode("ascii")}}
        else:
            if not text or len(text) > 10_000:
                raise ServiceError("Safety text must contain 1–10000 Unicode code points.")
            kind, body = "text", {"text": text}
        body["categories"] = sorted(CATEGORIES)
        body["outputType"] = "FourSeverityLevels"
        result = self.document(self.request(
            "POST", f"{endpoint}/contentsafety/{kind}:analyze?api-version=2024-09-01", json=body,
        ))
        entries = result.get("categoriesAnalysis", [])
        if (
            not isinstance(entries, list)
            or len(entries) != 4
            or any(not isinstance(entry, dict) for entry in entries)
            or {entry.get("category") for entry in entries} != CATEGORIES
            or any(type(entry.get("severity")) is not int or entry["severity"] not in (0, 2, 4, 6) for entry in entries)
        ):
            raise ServiceError("Content Safety returned incomplete category/severity evidence.")
        return result

    def collect(self, video_url: str | None, frames: list[tuple[float, Path]], required_text: list[str],
                *, recognized_transcript: str | None = None) -> dict:
        """Return checks + raw evidence; errors are explicit, isolated per service."""
        self.deadline = time.monotonic() + self.settings.deadline_seconds
        evidence: dict = {"generated": {}, "ocr": [], "safety_images": [], "safety_text": []}
        checks: list[dict] = []

        def check(name: str, state: str, detail: str):
            checks.append({"id": name, "state": state, "detail": detail})

        if not self.settings.enabled:
            for name in ("campaign_metadata", "mandatory_text", "image_safety", "text_safety"):
                check(name, "not-evaluated", "Paid Azure AI is disabled; no service call was made.")
            return {"evidence": evidence, "checks": checks}

        transcript_parts: list[str] = [recognized_transcript] if recognized_transcript else []
        if self.settings.understanding_endpoint and video_url:
            try:
                result = self.understanding(video_url)
                evidence["generated"]["content_understanding"] = result
                # Only documented transcript phrases feed text safety, never generated summaries.
                for content in result["result"]["contents"]:
                    for phrase in content.get("transcriptPhrases", []):
                        text = phrase.get("text")
                        if isinstance(text, str) and not recognized_transcript:
                            transcript_parts.append(text)
                check("campaign_metadata", "needs-review", "AI-generated campaign fields require human verification; raw service output retained.")
                if result["result"].get("warnings"):
                    check("understanding_warnings", "needs-review", "Content Understanding returned warnings; inspect raw evidence.")
            except ServiceError as exc:
                check("campaign_metadata", "not-evaluated", str(exc))
        else:
            check("campaign_metadata", "not-evaluated", "Content Understanding endpoint or signed video input is unconfigured.")

        selected = frames[:self.settings.max_frames]
        ocr_text: list[str] = []
        ocr_failed = False
        if self.settings.ocr_endpoint and selected:
            for timestamp, frame in selected:
                try:
                    raw = self.ocr(frame)
                    evidence["ocr"].append({"timestamp_seconds": timestamp, "raw": raw})
                    ocr_text.append(raw["analyzeResult"]["content"])
                except ServiceError as exc:
                    evidence["ocr"].append({"timestamp_seconds": timestamp, "error": str(exc)})
                    ocr_failed = True
            observed = "\n".join(ocr_text).casefold()
            evidence["mandatory_text_observations"] = [
                {"text": text, "observed_in_sample": text.casefold() in observed} for text in required_text
            ]
            check(
                "mandatory_text", "needs-review" if not ocr_failed else "not-evaluated",
                "OCR applies only to sampled timestamps. Presence, absence, legibility and display duration need human review."
                if not ocr_failed else "At least one sampled-frame OCR operation failed; evidence is incomplete.",
            )
        else:
            check("mandatory_text", "not-evaluated", "Read OCR endpoint unconfigured or no sampled frames available.")

        def safety_state(records: list[dict]) -> str:
            if any("raw" in row and any(entry["severity"] >= 4 for entry in row["raw"]["categoriesAnalysis"]) for row in records):
                return "fail"
            if not records or any("error" in row for row in records):
                return "not-evaluated"
            return "needs-review"

        if self.settings.safety_endpoint and selected:
            for timestamp, frame in selected:
                try:
                    raw = self.safety(frame=frame)
                    evidence["safety_images"].append({"timestamp_seconds": timestamp, "raw": raw})
                except ServiceError as exc:
                    evidence["safety_images"].append({"timestamp_seconds": timestamp, "error": str(exc)})
            check("image_safety", safety_state(evidence["safety_images"]), "Illustrative threshold >=4 flags failure. Sampling cannot clear the entire video.")
        else:
            check("image_safety", "not-evaluated", "Content Safety endpoint unconfigured or no sampled frames available.")

        text = "\n".join(transcript_parts + ocr_text)
        if text and self.settings.safety_endpoint:
            bounded = text[:self.settings.max_text_chars]
            for offset in range(0, len(bounded), 10_000):
                try:
                    raw = self.safety(text=bounded[offset:offset + 10_000])
                    evidence["safety_text"].append({"offset": offset, "characters": len(bounded[offset:offset + 10_000]), "raw": raw})
                except ServiceError as exc:
                    evidence["safety_text"].append({"offset": offset, "error": str(exc)})
            evidence["text_coverage"] = {
                "input_characters": len(text), "analyzed_character_limit": self.settings.max_text_chars,
                "truncated": len(text) > len(bounded), "transcript_observed": bool(transcript_parts),
                "source": "Recognized transcript phrases and sampled OCR, not generated campaign descriptions.",
            }
            check("text_safety", safety_state(evidence["safety_text"]), "Text is recognized evidence, not full audiovisual coverage. Human review required.")
        else:
            check("text_safety", "not-evaluated", "No recognized transcript/OCR text available, or safety endpoint unconfigured.")
        evidence["sampling"] = {
            "timestamps_seconds": [stamp for stamp, _ in selected], "full_video_coverage": False,
            "frames_truncated": len(frames) > len(selected),
        }
        return {"evidence": evidence, "checks": checks}
