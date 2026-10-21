from copy import deepcopy
from dataclasses import replace
from pathlib import Path
import shutil
import subprocess
import time

import httpx
import pytest

from videoextration.ai import ServiceError
from videoextration.media import MediaError
from videoextration.transcription import normalize_transcript, transcribe_video


RAW = {
    "durationMilliseconds": 4000,
    "combinedPhrases": [{"text": "Try our new campaign."}],
    "phrases": [{
        "offsetMilliseconds": 250, "durationMilliseconds": 1500,
        "text": "Try our new campaign.", "locale": "en-US", "confidence": 0.95,
    }],
}


class Credential:
    def get_token(self, scope):
        assert scope == "https://cognitiveservices.azure.com/.default"
        return type("Token", (), {"token": "test-only-token"})()


def test_normalized_text_has_audio_timestamps_and_raw_evidence():
    result = normalize_transcript(RAW)
    assert result["text"] == "Try our new campaign."
    assert result["segments"] == [{
        "start_seconds": 0.25, "end_seconds": 1.75,
        "text": "Try our new campaign.", "locale": "en-US",
    }]
    assert result["raw"] == RAW
    assert result["state"] == "needs-review"
    assert result["human_verified"] is False


def test_silence_is_not_a_claim_that_speech_is_absent():
    result = normalize_transcript({"combinedPhrases": [], "phrases": []})
    assert result["text"] == "" and result["state"] == "needs-review"
    assert "does not prove" in result["detail"]


@pytest.mark.parametrize("invalid", [None, [], "0", True, -1, float("nan"), float("inf")])
def test_invalid_timestamps_are_rejected(invalid):
    raw = deepcopy(RAW)
    raw["phrases"][0]["offsetMilliseconds"] = invalid
    with pytest.raises(ServiceError, match="timestamps"):
        normalize_transcript(raw)


@pytest.mark.parametrize("raw", [
    {}, {"combinedPhrases": [], "phrases": None},
    {"combinedPhrases": [{"text": 123}], "phrases": []},
    {"combinedPhrases": [], "phrases": [None]},
    {"combinedPhrases": [{"text": "Speech"}], "phrases": []},
])
def test_incomplete_service_response_is_not_a_success(raw):
    with pytest.raises(ServiceError):
        normalize_transcript(raw)


@pytest.mark.parametrize("enabled,endpoint,audio", [
    ("false", "https://speech.example", True),
    ("true", "", True),
    ("true", "https://speech.example", False),
])
def test_disabled_missing_config_and_no_audio_never_extract_or_call(monkeypatch, settings, enabled, endpoint, audio):
    monkeypatch.setenv("ENABLE_PAID_AI", enabled)
    monkeypatch.setenv("SPEECH_ENDPOINT", endpoint)
    monkeypatch.setattr("videoextration.transcription.bounded_command", lambda *args: pytest.fail("Audio extracted"))
    result = transcribe_video(Path("unused"), {"has_audio": audio}, settings)
    assert result["state"] == "not-evaluated"
    assert result["text"] is None and result["segments"] == []


def test_fast_speech_multipart_contract_and_bounded_extraction(monkeypatch, settings):
    monkeypatch.setenv("ENABLE_PAID_AI", "true")
    monkeypatch.setenv("SPEECH_ENDPOINT", "https://speech.example")
    monkeypatch.setenv("SPEECH_LOCALES", "en-US, fr-FR")
    calls = []
    def extract(args, deadline, output_limit):
        calls.append(args)
        assert 0 < deadline - time.monotonic() <= 60
        assert output_limit == 120 * 32000 + 131072
        return b"RIFFtestaudio"
    monkeypatch.setattr("videoextration.transcription.bounded_command", extract)
    def remote(request):
        assert request.url.path == "/speechtotext/transcriptions:transcribe"
        assert request.url.params["api-version"] == "2025-10-15"
        assert request.headers["authorization"].endswith("test-only-token")
        body = request.read()
        assert b'name="audio"; filename="audio.wav"' in body
        assert b"RIFFtestaudio" in body and b'"locales": ["en-US", "fr-FR"]' in body
        assert b'"profanityFilterMode": "None"' in body
        return httpx.Response(200, json=RAW)
    with httpx.Client(transport=httpx.MockTransport(remote)) as client:
        result = transcribe_video("source.mp4", {"has_audio": True}, settings, credential=Credential(), client=client)
        assert not client.is_closed
    assert result["text"] == "Try our new campaign."
    assert calls[0][calls[0].index("-protocol_whitelist") + 1] == "file"
    assert calls[0][calls[0].index("-map") + 1] == "0:a:0"
    assert calls[0][calls[0].index("-t") + 1] == "120.0"


@pytest.mark.parametrize("status", [400, 401, 403, 429, 500])
def test_service_errors_are_explicit_without_leaking_credentials(monkeypatch, settings, status):
    monkeypatch.setenv("ENABLE_PAID_AI", "true")
    monkeypatch.setenv("SPEECH_ENDPOINT", "https://speech.example")
    monkeypatch.setattr("videoextration.transcription.bounded_command", lambda *args: b"audio")
    with httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(status))) as client:
        result = transcribe_video("source", {"has_audio": True}, settings, credential=Credential(), client=client)
    assert result["state"] == "not-evaluated" and result["text"] is None
    assert f"HTTP {status}" in result["detail"]
    assert "test-only-token" not in str(result)


def test_bad_audio_is_not_sent(monkeypatch, settings):
    monkeypatch.setenv("ENABLE_PAID_AI", "true")
    monkeypatch.setenv("SPEECH_ENDPOINT", "https://speech.example")
    def fail(*args):
        raise MediaError("invalid-or-unsupported-media")
    monkeypatch.setattr("videoextration.transcription.bounded_command", fail)
    result = transcribe_video("source", {"has_audio": True}, settings)
    assert "Audio extraction failed" in result["detail"]
    assert result["text"] is None


@pytest.mark.skipif(not shutil.which("ffmpeg"), reason="FFmpeg required")
def test_real_audio_extraction_without_paid_inference(monkeypatch, settings, tmp_path):
    video = tmp_path / "audio.mp4"
    subprocess.run([
        "ffmpeg", "-nostdin", "-v", "error", "-f", "lavfi", "-i", "sine=frequency=440:duration=1",
        "-c:a", "aac", str(video),
    ], check=True, timeout=10)
    monkeypatch.setenv("ENABLE_PAID_AI", "true")
    monkeypatch.setenv("SPEECH_ENDPOINT", "https://speech.example")
    def remote(request):
        assert b"RIFF" in request.read() and b"WAVE" in request.read()
        return httpx.Response(200, json={"combinedPhrases": [], "phrases": []})
    with httpx.Client(transport=httpx.MockTransport(remote)) as client:
        result = transcribe_video(video, {"has_audio": True}, replace(settings, max_duration_seconds=2), credential=Credential(), client=client)
    assert result["text"] == "" and result["state"] == "needs-review"
