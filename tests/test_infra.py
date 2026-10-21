"""Compile-only infrastructure contract tests; never call a deployment API."""

import json
import shutil
import subprocess
from pathlib import Path

import pytest


@pytest.fixture(scope="module")
def arm():
    if not shutil.which("az"):
        pytest.skip("Azure CLI/Bicep required for compile-only infrastructure contracts")
    root = Path(__file__).resolve().parents[1]
    output = subprocess.check_output([
        "az", "bicep", "build", "--file", str(root / "infra/main.bicep"), "--stdout",
    ], timeout=60)
    return json.loads(output)


def resources(template):
    value = template["resources"]
    return list(value.values()) if isinstance(value, dict) else value


def child_template(arm):
    return next(resource for resource in resources(arm) if resource["type"] == "Microsoft.Resources/deployments")["properties"]["template"]


def find(arm, kind):
    return next(resource for resource in resources(child_template(arm)) if resource["type"] == kind)


def test_compute_is_opt_in_and_auth_config_is_required(arm):
    assert arm["parameters"]["deployCompute"]["defaultValue"] is False
    assert arm["parameters"]["enablePaidAi"]["defaultValue"] is False
    assert arm["parameters"]["apiAudience"]["minLength"] == 1
    module = next(resource for resource in resources(arm) if resource["type"] == "Microsoft.Resources/deployments")
    image = json.dumps(module["properties"]["parameters"]["containerImage"])
    assert "videoextraction:" in image and "https://" not in image


def test_both_compute_targets_pull_private_images_and_use_numeric_cpu(arm):
    for kind in ("Microsoft.App/containerApps", "Microsoft.App/jobs"):
        target = find(arm, kind)
        assert "deployCompute" in target["condition"]
        assert target["properties"]["configuration"]["registries"][0]["identity"]
        cpu = target["properties"]["template"]["containers"][0]["resources"]["cpu"]
        assert isinstance(cpu, (int, float)) or cpu == "[json('0.5')]"
    job = find(arm, "Microsoft.App/jobs")
    assert job["properties"]["configuration"]["replicaTimeout"] == 1200
    rule = job["properties"]["configuration"]["eventTriggerConfig"]["scale"]["rules"][0]
    assert rule["identity"] and rule["metadata"]["queueName"] == "analysis"


def test_storage_is_private_and_upload_contract_and_cleanup_exist(arm):
    storage = find(arm, "Microsoft.Storage/storageAccounts")
    assert storage["properties"]["allowBlobPublicAccess"] is False
    assert storage["properties"]["allowSharedKeyAccess"] is False
    assert storage["properties"]["supportsHttpsTrafficOnly"] is True
    blob = find(arm, "Microsoft.Storage/storageAccounts/blobServices")
    cors_expression = json.dumps(blob["properties"]["cors"])
    assert "x-ms-blob-type" in cors_expression and "if-none-match" in cors_expression
    policy = find(arm, "Microsoft.Storage/storageAccounts/managementPolicies")
    rules = policy["properties"]["policy"]["rules"]
    assert len(rules) == 2
    assert rules[0]["definition"]["filters"]["prefixMatch"] == ["uploads/"]
    assert rules[1]["definition"]["filters"]["prefixMatch"] == ["reports/", "jobs/"]
    registry = find(arm, "Microsoft.ContainerRegistry/registries")
    assert registry["sku"]["name"] == "Basic" and "policies" not in registry["properties"]


def test_speech_is_optional_and_wired_only_to_the_worker(arm):
    assert arm["parameters"]["speechEndpoint"]["defaultValue"] == ""
    assert arm["parameters"]["speechLocales"]["defaultValue"] == ""
    job = find(arm, "Microsoft.App/jobs")
    environment = {item["name"]: item["value"] for item in job["properties"]["template"]["containers"][0]["env"]}
    assert environment["SPEECH_ENDPOINT"] == "[parameters('speechEndpoint')]"
    assert environment["SPEECH_LOCALES"] == "[parameters('speechLocales')]"
