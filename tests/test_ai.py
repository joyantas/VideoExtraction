from pathlib import Path

import httpx
import pytest

from videoextration.ai import AISettings, AzureAI, ServiceError


class Credential:
    def get_token(self, scope):
        assert scope == "https://cognitiveservices.azure.com/.default"
        return type("Token", (), {"token": "test-only-token"})()


def test_disabled_never_calls_remote():
    def unexpected(request):
        raise AssertionError("Paid API called while disabled")
    client = AzureAI(AISettings(), Credential(), httpx.Client(transport=httpx.MockTransport(unexpected)))
    result = client.collect(None, [], ["Buy now"])
    assert all(c["state"] == "not-evaluated" for c in result["checks"])


def test_success_is_still_unverified_and_ocr_is_sampled(tmp_path):
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"jpeg-test-fixture")

    def remote(request):
        assert request.headers["authorization"] == "Bearer test-only-token"
        path = request.url.path
        if path.endswith(":analyze") and "contentunderstanding" in path:
            assert request.read().find(b'"inputs"') != -1
            return httpx.Response(202, headers={"Operation-Location": "https://cu.example/result"})
        if request.url.host == "cu.example" and path == "/result":
            return httpx.Response(200, json={
                "status": "Succeeded", "result": {"contents": [{
                    "fields": {"brand": {"valueString": "Unverified"}},
                    "transcriptPhrases": [{"text": "Hello campaign"}],
                }]},
            })
        if "documentModels" in path:
            return httpx.Response(202, headers={"Operation-Location": "https://ocr.example/result"})
        if request.url.host == "ocr.example":
            return httpx.Response(200, json={"status": "succeeded", "analyzeResult": {"content": "Buy now"}})
        return httpx.Response(200, json={"categoriesAnalysis": [
            {"category": c, "severity": 0} for c in ("Hate", "SelfHarm", "Sexual", "Violence")
        ]})

    settings = AISettings(True, "https://cu.example", "campaign", "https://safe.example", "https://ocr.example")
    client = AzureAI(settings, Credential(), httpx.Client(transport=httpx.MockTransport(remote)))
    result = client.collect("https://storage.example/read-signed", [(1.2, frame)], ["Buy now", "Missing"])
    assert all(c["state"] == "needs-review" for c in result["checks"])
    assert result["evidence"]["sampling"]["full_video_coverage"] is False
    assert result["evidence"]["mandatory_text_observations"] == [
        {"text": "Buy now", "observed_in_sample": True}, {"text": "Missing", "observed_in_sample": False},
    ]


@pytest.mark.parametrize("payload", [
    {}, {"categoriesAnalysis": []},
    {"categoriesAnalysis": [{"category": "Hate", "severity": 0}]},
])
def test_incomplete_safety_is_not_evaluated(payload, tmp_path):
    frame = tmp_path / "frame.jpg"
    frame.write_bytes(b"test")
    client = AzureAI(
        AISettings(enabled=True, safety_endpoint="https://safe.example"), Credential(),
        httpx.Client(transport=httpx.MockTransport(lambda request: httpx.Response(200, json=payload))),
    )
    result = client.collect(None, [(0, frame)], [])
    assert next(c for c in result["checks"] if c["id"] == "image_safety")["state"] == "not-evaluated"
    assert "error" in result["evidence"]["safety_images"][0]


def test_off_origin_poll_is_rejected():
    client = AzureAI(AISettings(), Credential())
    with pytest.raises(ServiceError, match="off-origin"):
        client.poll(httpx.Response(202, headers={"Operation-Location": "https://attacker.example/x"}), "https://cu.example")


def test_recognized_speech_text_can_feed_safety_without_content_understanding():
    def remote(request):
        assert b"Recognized audio words" in request.read()
        return httpx.Response(200, json={"categoriesAnalysis": [
            {"category": c, "severity": 0} for c in ("Hate", "SelfHarm", "Sexual", "Violence")
        ]})
    client = AzureAI(
        AISettings(enabled=True, safety_endpoint="https://safe.example"), Credential(),
        httpx.Client(transport=httpx.MockTransport(remote)),
    )
    result = client.collect(None, [], [], recognized_transcript="Recognized audio words")
    assert result["evidence"]["text_coverage"]["transcript_observed"] is True
    assert next(check for check in result["checks"] if check["id"] == "text_safety")["state"] == "needs-review"


@pytest.mark.parametrize("endpoint", ["http://x", "https://x/path", "https://user:secret@x", "https://x?secret=1"])
def test_endpoint_requires_resource_origin(endpoint):
    with pytest.raises(ServiceError):
        AzureAI.endpoint(endpoint)
