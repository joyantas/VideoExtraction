"""Validate analyzer-supported models; create the custom analyzer only with --apply."""

import argparse
import json
import time

from azure.identity import DefaultAzureCredential

from videoextration.ai import AISettings, AzureAI, ServiceError


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--endpoint", required=True)
    parser.add_argument("--completion-model", required=True)
    parser.add_argument("--completion-deployment", required=True)
    parser.add_argument("--embedding-model", required=True)
    parser.add_argument("--embedding-deployment", required=True)
    parser.add_argument("--analyzer-id", default="campaign-video-v1")
    parser.add_argument("--apply", action="store_true", help="Create analyzer configuration; never deploys models or runs inference.")
    args = parser.parse_args()
    client = AzureAI(AISettings(), DefaultAzureCredential())
    client.deadline = time.monotonic() + 120
    endpoint = client.endpoint(args.endpoint)
    base_url = f"{endpoint}/contentunderstanding/analyzers/prebuilt-video?api-version=2025-11-01"
    base = client.document(client.request("GET", base_url))
    supported = base.get("supportedModels", {})
    for role, chosen in (("completion", args.completion_model), ("embedding", args.embedding_model)):
        if chosen not in supported.get(role, []):
            raise ServiceError(f"Model {chosen!r} is not listed in this resource's prebuilt-video supportedModels.{role}.")
    if not 1 <= len(args.analyzer_id) <= 64 or any(
        c not in "abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789._-" for c in args.analyzer_id
    ):
        raise ValueError("Invalid analyzer ID.")
    definition = {
        "baseAnalyzerId": "prebuilt-video",
        "description": "Campaign metadata hypotheses for human review, not compliance certification.",
        "models": {"completion": args.completion_model, "embedding": args.embedding_model},
        "config": {"returnDetails": True, "enableSegment": False},
        "fieldSchema": {"name": "CampaignVideoV1", "fields": {
            "Brand": {"type": "string", "method": "generate", "description": "Brand promoted; say unknown if not discernible."},
            "Offer": {"type": "string", "method": "generate", "description": "Apparent campaign offer; preserve uncertainty."},
            "CallToAction": {"type": "string", "method": "generate", "description": "Apparent call to action, if any."},
            "Summary": {"type": "string", "method": "generate", "description": "Brief audiovisual campaign summary. Do not claim legal or publishing approval."},
        }},
    }
    mappings = {args.completion_model: args.completion_deployment, args.embedding_model: args.embedding_deployment}
    print(json.dumps({"analyzer": definition, "CU_MODEL_DEPLOYMENTS": mappings}, indent=2))
    if args.apply:
        response = client.request(
            "PUT", f"{endpoint}/contentunderstanding/analyzers/{args.analyzer_id}?api-version=2025-11-01&allowReplace=false",
            json=definition,
        )
        print(f"Analyzer creation accepted (HTTP {response.status_code}).")
        print("GET the analyzer until status is ready before enabling analysis. No inference was invoked.")
    else:
        print("Read-only validation complete. Add --apply only after approving creation of this analyzer.")


if __name__ == "__main__":
    main()
