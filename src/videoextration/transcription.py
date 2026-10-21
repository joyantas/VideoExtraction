"""Opt-in Azure Speech recognition; audio is never sent while paid AI is disabled."""

import json
import logging
import math
import os
import time
from pathlib import Path

from azure.identity import AzureCliCredential, ManagedIdentityCredential

from .ai import AISettings, AzureAI, ServiceError
from .media import MediaError, bounded_command


log = logging.getLogger(__name__)


def empty_transcript(detail="Audio transcription is unconfigured; no recognition was performed."):
    return {
        "state": "not-evaluated", "text": None, "segments": [],
        "source": "azure-speech", "human_verified": False, "detail": detail,
        "timestamp_reference": "Start of the extracted first audio stream.",
    }


def normalize_transcript(raw):
    combined, phrases = raw.get("combinedPhrases"), raw.get("phrases")
    if not isinstance(combined, list) or not isinstance(phrases, list):
        raise ServiceError("Azure Speech returned an incomplete transcript.")
    if any(not isinstance(row, dict) or not isinstance(row.get("text"), str) for row in combined):
        raise ServiceError("Azure Speech returned invalid combined phrases.")
    segments = []
    for phrase in phrases:
        if not isinstance(phrase, dict) or not isinstance(phrase.get("text"), str):
            raise ServiceError("Azure Speech returned an invalid phrase.")
        offset, duration = phrase.get("offsetMilliseconds"), phrase.get("durationMilliseconds")
        if any(type(value) not in (int, float) or not math.isfinite(value) or value < 0
               for value in (offset, duration)) or not math.isfinite(offset + duration):
            raise ServiceError("Azure Speech returned invalid phrase timestamps.")
        segment = {
            "start_seconds": offset / 1000,
            "end_seconds": (offset + duration) / 1000,
            "text": phrase["text"],
        }
        if isinstance(phrase.get("locale"), str):
            segment["locale"] = phrase["locale"]
        segments.append(segment)
    text = "\n".join(row["text"] for row in combined).strip()
    if bool(text) != any(row["text"].strip() for row in segments):
        raise ServiceError("Azure Speech returned inconsistent text and phrase evidence.")
    result = empty_transcript()
    result.update(
        state="needs-review", text=text, segments=segments, raw=raw,
        detail="Machine-recognized speech; verify accuracy against the audio."
        if text else "No speech was recognized; this does not prove the audio contains no speech.",
    )
    return result


def transcribe_video(path, metadata, settings, *, credential=None, client=None):
    if not metadata.get("has_audio"):
        return empty_transcript("No audio stream was detected; no speech service was called.")
    if os.getenv("ENABLE_PAID_AI", "false").lower() != "true":
        return empty_transcript("Paid Azure AI is disabled; no audio was sent to Azure Speech.")
    endpoint = os.getenv("SPEECH_ENDPOINT", "")
    if not endpoint:
        return empty_transcript("SPEECH_ENDPOINT is unconfigured; no audio was sent to Azure Speech.")
    try:
        # Reserve the media budget and persistence margin within the local queue lease.
        deadline = time.monotonic() + min(60, settings.lease_seconds - settings.media_wall_seconds - 30)
        endpoint = AzureAI.endpoint(endpoint)
        audio = bounded_command([
            settings.ffmpeg_binary, "-nostdin", "-v", "error", "-threads", "1",
            "-protocol_whitelist", "file", "-format_whitelist", "mov,matroska,webm,avi",
            "-probesize", "5000000", "-analyzeduration", "5000000", "-max_alloc", "33554432",
            "-i", str(Path(path).resolve()), "-map", "0:a:0", "-vn", "-sn", "-dn",
            "-t", str(settings.max_duration_seconds), "-ac", "1", "-ar", "16000",
            "-c:a", "pcm_s16le", "-f", "wav", "pipe:1",
        ], deadline, int(settings.max_duration_seconds * 32000) + 131072)
        if not audio:
            raise ServiceError("Audio extraction returned no data.")
        if credential is None:
            credential = (
                ManagedIdentityCredential(client_id=os.getenv("AZURE_CLIENT_ID") or None)
                if settings.mode == "azure" else AzureCliCredential(
                    tenant_id=os.getenv("AZURE_TENANT_ID") or None, process_timeout=10,
                )
            )
        service = AzureAI(AISettings(), credential, client)
        service.deadline = deadline
        locales = [value.strip() for value in os.getenv("SPEECH_LOCALES", "").split(",") if value.strip()]
        definition = {"locales": locales} if locales else {}
        definition["profanityFilterMode"] = "None"
        try:
            response = service.request(
                "POST", f"{endpoint}/speechtotext/transcriptions:transcribe?api-version=2025-10-15",
                files={
                    "audio": ("audio.wav", audio, "audio/wav"),
                    "definition": (None, json.dumps(definition), "application/json"),
                },
            )
            return normalize_transcript(service.document(response))
        finally:
            if client is None:
                service.client.close()
    except (MediaError, ServiceError) as exc:
        detail = f"Audio extraction failed: {exc.code}." if isinstance(exc, MediaError) else str(exc)
        log.warning("Audio transcription unavailable: %s", detail)
        return empty_transcript(detail)
