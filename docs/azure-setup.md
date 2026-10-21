# Azure setup (manual, separately approved)

**Nothing in this project has been deployed.** Generating these files does not
authorize resource provisioning, model deployment, analyzer creation, or paid
analysis. The subscription target is
`acb0b35a-6cfe-4bfc-b459-b9bb61f3e4ca`. Do not change shared Azure CLI defaults:
use `--subscription` explicitly on resource-management commands.

## Identity and existing AI prerequisites

1. Create or select a single-tenant Entra API app registration. Expose a delegated
   scope named `Video.Analyze`, configure v2 access tokens
   (`requestedAccessTokenVersion: 2`), and authorize your browser/OAuth client.
   Entra registration/tenant changes are deliberately not automated by the sample.
2. Configure `AZURE_TENANT_ID`, `AZURE_API_AUDIENCE` (the actual `aud` in issued
   access tokens), and `AZURE_REQUIRED_SCOPE=Video.Analyze`. For a v2 API the
   audience is commonly the API client ID; do not assume the Application ID URI
   without checking the issued token. Never use an ID token as a bearer API token.
3. The sample UI accepts an API access token obtained through your approved OAuth
   authorization-code + PKCE client. It keeps that token in page memory only.
   It is a sample token-entry UI, not a complete Entra login application. Add MSAL
   and the registration's exact redirect URI before a production rollout.
4. Use existing, appropriately located Foundry/Content Understanding, Content
   Safety, and Document Intelligence resources. No models are deployed
   automatically. Endpoint settings are HTTPS origins, not portal URLs or keys.
5. Grant the deployed worker identity the applicable data-plane role on each
   selected AI resource (for these REST endpoints, typically **Cognitive Services
   User**). Confirm the current service-specific RBAC requirements in your tenant.
   The analyzer setup operator also needs analyzer-creation permissions. Give the
   Foundry resource identity access to connected model deployments as required by
   the [model deployment guide](https://learn.microsoft.com/en-us/azure/ai-services/content-understanding/concepts/models-deployments).
   These are separate from the API's Storage/ACR role assignments.

Do not add API keys to Bicep, parameter files, JavaScript, or source control.
Server integrations use managed identity, not the end user's browser token.

## Content Understanding analyzer

The GA API is `2025-11-01`. `scripts/configure_analyzer.py` defines the sample's
custom Brand, Offer, CallToAction and Summary fields. They are generated
hypotheses, not factual clearance evidence.

The script first reads `prebuilt-video` and checks its **live**
`supportedModels.completion` and `supportedModels.embedding` lists. Supply model
names and **existing deployment names** from your actual Foundry resource. No
example model name is assumed to be supported by every analyzer, region or
subscription.

From the activated project environment:

```bash
PYTHONPATH=src python scripts/configure_analyzer.py \
  --endpoint "https://YOUR-RESOURCE.cognitiveservices.azure.com" \
  --completion-model "SUPPORTED-MODEL-NAME" \
  --completion-deployment "EXISTING-COMPLETION-DEPLOYMENT" \
  --embedding-model "SUPPORTED-EMBEDDING-MODEL-NAME" \
  --embedding-deployment "EXISTING-EMBEDDING-DEPLOYMENT"
```

Without `--apply`, this only reads the analyzer and prints the definition and
model mapping. It does not create anything or invoke inference. If a model is not
listed, stop and choose a supported deployment; don't bypass the check. An
embedding deployment can be a Content Understanding service dependency; this
sample does not add a vector store or embedding pipeline.

**After separate approval**, repeat the command with `--apply` to create the
custom analyzer. It refuses to replace an existing analyzer. Poll its GET
endpoint until status is `ready`; a creation-accepted response is not readiness.
Set `CU_ANALYZER_ID=campaign-video-v1` and set `CU_MODEL_DEPLOYMENTS` to the printed
JSON mapping of model names to deployment names. This mapping is sent in each
analyze request rather than modifying shared resource defaults.

## AI configuration and costs

| Setting | Purpose |
|---|---|
| `ENABLE_PAID_AI` | `false` by default. Set `true` only after explicit cost approval. |
| `CU_ENDPOINT` | Existing Content Understanding/Foundry resource endpoint |
| `CU_ANALYZER_ID` | Ready custom video analyzer, default `campaign-video-v1` |
| `CU_MODEL_DEPLOYMENTS` | JSON model-name-to-existing-deployment mapping |
| `OCR_ENDPOINT` | Existing Document Intelligence endpoint, Read API `2024-11-30` |
| `SAFETY_ENDPOINT` | Existing Content Safety endpoint, API `2024-09-01` |
| `SPEECH_ENDPOINT` | Optional approved Speech resource custom-domain origin for fast transcription, API `2025-10-15` |
| `SPEECH_LOCALES` | Optional comma-separated speech locales; empty uses language identification |

Optional audio transcripts use `ENABLE_PAID_AI=true` plus a nonempty
`SPEECH_ENDPOINT`. Configure `speechEndpoint` and `speechLocales` in Bicep for
the hosted worker. For local Mac operation the worker explicitly uses
`AzureCliCredential`; hosted operation uses managed identity, without falling
back to a developer login. The selected caller must have the applicable Speech
data-plane permission (for example Cognitive Services Speech User) on the
approved resource; no role assignments are made implicitly.

The first audio stream is extracted to bounded mono 16 kHz PCM in memory and
sent as multipart audio, not a public URL. New report JSON includes
`transcript.text`, `transcript.segments` and raw recognition evidence; segment
times refer to the extracted audio's start. Recognition is always unverified
and `needs-review`, even for an empty successful recognition result. Missing
audio/configuration, failed extraction/authentication and service errors
produce explicit `not-evaluated` results with `text: null`. Existing reports are
not silently reprocessed or charged. When the hosted text-safety pipeline is
also enabled, recognized Speech text can feed it without being treated as
AI-generated campaign prose.

Public West Europe retail pricing retrieved on 2026-10-21 lists ordinary Fast
Transcription Speech To Text at USD 0.36/audio-hour (about USD 0.006/minute),
excluding service rounding, retries, other configured AI services and normal
compute/storage charges. This is not a spending cap or a quote. A Speech
endpoint setting does not approve paid calls; obtain explicit cost/data-transfer
approval before enabling it for uploaded campaign material. No live inference
is required by the automated tests; they mock service responses, even when
testing real local audio extraction.

Reference: [Speech fast transcription REST contract and keyless authentication](https://learn.microsoft.com/en-us/azure/ai-services/speech-service/fast-transcription-create).

Unconfigured endpoints and failed calls produce `not-evaluated`, not pass.
Successful semantic and sampled safety/OCR results still require review.
Sampling limits apply separately to Content Understanding and locally extracted
frames. Service errors are reported without logging bearer tokens or SAS URLs.

Live public retail pricing was consulted on 2026-10-20 for East US 2:
ACR Basic was USD 0.1666/day (about USD 5 for 30 days). Container Apps Standard
active CPU was USD 0.000024/vCPU-second; other memory/request meters also apply.
These are **not** a total monthly estimate or a quote. Storage/transactions,
logging, network egress, builds, all AI service usage, and model tokens are
additional. Grants are shared at subscription scope and may already be consumed.
Check current prices and quota before deployment. Do not interpret scale-to-zero
as a guaranteed free service.

## Storage and queue behavior

Storage shared-key auth and anonymous Blob access must be disabled. Configure
browser CORS only for the deployed frontend's exact HTTPS origin (plus explicit
local origins if intentionally needed), allow PUT/OPTIONS and the headers
`content-type`, `x-ms-blob-type`, and `if-none-match`; never use a wildcard origin.
Storage containers remain private even though this sample uses public network
endpoints.

The API returns a ten-minute, create-only user-delegation SAS for a random object.
Finalization requires the authenticated owner, validates size/type, and records
the ETag. Workers download only that ETag and independently inspect media bytes.
A token cannot grant a hard pre-upload byte cap: oversized/rejected/orphaned
objects still consume storage until deleted. Keep lifecycle cleanup enabled and
add application quotas/rate limiting for production.

If queue delivery fails, the job remains `queue-pending` and the API returns an
explicit error. Retry the owner's `/complete` operation to repair delivery.
An operational outbox reconciler is a documented production addition, not hidden
inside this sample. Queue retries are bounded; inspect `analysis-poison` and
failed job errors rather than assuming every execution produced a report.

The template expires uploads after seven days and job records/reports after
30 days by default; `uploadsRetentionDays` and `reportsRetentionDays` configure
these lifecycle rules. Lifecycle deletion is asynchronous, not an exact
expiry-time guarantee. The sample does not enable Blob soft-delete recovery.
Do not manually delete an active job, upload, or report. Review retention and
data-processing geography before using real campaign material.

## Manual infrastructure and image deployment

These commands are **examples for an operator after separate deployment
approval**. They have not been executed. The generated Bicep uses subscription
scope; the default parameter file selects the sample names and East US 2.
Confirm quota, policy, role-assignment privileges, budget and region suitability
before running anything that creates resources. Using a different region may
also require selecting different existing AI resources/model deployments.

Compile locally first (does not provision resources):

```bash
az bicep build --file infra/main.bicep --stdout > /dev/null
```

The base phase intentionally does **not** deploy a placeholder public API or
worker. It creates the storage/queues, registry, identity, logs and environment.
Supply the real Entra API audience after completing the app-registration steps:

```bash
SUBSCRIPTION=acb0b35a-6cfe-4bfc-b459-b9bb61f3e4ca
API_AUDIENCE="YOUR-ACTUAL-API-AUDIENCE"

# OPTIONAL, read-only evaluation of the proposed template:
az deployment sub what-if --subscription "$SUBSCRIPTION" \
  --location eastus2 --template-file infra/main.bicep \
  --parameters @infra/main.parameters.json apiAudience="$API_AUDIENCE"

# CREATES RESOURCES AND INCURS CHARGES: separate approval required.
az deployment sub create --subscription "$SUBSCRIPTION" \
  --name videoextration-base --location eastus2 \
  --template-file infra/main.bicep \
  --parameters @infra/main.parameters.json apiAudience="$API_AUDIENCE" deployCompute=false

# BUILDS/PUSHES AN IMAGE AND MAY INCUR CHARGES: separate approval required.
az acr build --subscription "$SUBSCRIPTION" \
  --registry crvideoextradev1737 --image videoextration:sample .

# DEPLOYS THE REAL API/WORKER: separate approval required.
az deployment sub create --subscription "$SUBSCRIPTION" \
  --name videoextration-compute --location eastus2 \
  --template-file infra/main.bicep \
  --parameters @infra/main.parameters.json apiAudience="$API_AUDIENCE" \
    imageTag=sample deployCompute=true
```

Allow time for managed-identity RBAC propagation. A resource's ARM
`Succeeded` status does not prove the API is healthy or a worker can pull the
image/use the queue. Confirm the actual FQDN, HTTPS endpoints and worker
execution using a non-sensitive test video:

```bash
az containerapp show --subscription "$SUBSCRIPTION" \
  --resource-group rg-videoextra-dev-1737 --name ca-videoextra-dev-1737 \
  --query properties.configuration.ingress.fqdn -o tsv
```

Visit that exact HTTPS origin. The Blob service CORS configuration must match
it (the template derives the normal Container Apps origin; use `corsOrigin` for
an approved custom-domain override). Test `/healthz`, `/readyz`, authenticated
job creation, direct Blob PUT, finalization, queue execution and owner-scoped
report access. Verify skipped AI checks are visibly `not-evaluated`.

The shared image's Dockerfile has an API-oriented local Docker HEALTHCHECK.
Container Apps uses its explicit API probes; the non-HTTP job does not serve a
health endpoint. A queue worker exiting after one message is expected.

AI endpoints and mapping parameters are optional. After separately approving
inference costs and completing service/model/analyzer/RBAC setup, set the
`enablePaidAi` Bicep parameter to `true` when redeploying with `cuEndpoint`,
`cuAnalyzerId`, `cuModelDeployments`, `ocrEndpoint` and `safetyEndpoint`.
No model deployment or analyzer creation is performed by the infrastructure
template. Until then, technical reports work and AI checks remain unevaluated.

For cleanup **only after approving deletion of all sample data/resources**:

```bash
az group delete --subscription "$SUBSCRIPTION" \
  --name rg-videoextra-dev-1737
```

This deletes the entire sample group, including reports and uploads; it is
not an operation the agent has run. Existing AI resources and Entra registrations
outside the group are not removed by that command.
