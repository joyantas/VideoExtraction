import base64
from types import SimpleNamespace
from urllib.parse import parse_qs, urlparse
from uuid import uuid4

from azure.storage.blob import UserDelegationKey

from videoextration.azure_store import AzureStore


def test_real_sdk_upload_sas_has_only_create_permission_for_one_blob():
    key = UserDelegationKey()
    key.signed_oid, key.signed_tid = str(uuid4()), str(uuid4())
    key.signed_start, key.signed_expiry = "2026-10-20T00:00:00Z", "2026-10-21T00:00:00Z"
    key.signed_service, key.signed_version = "b", "2023-11-03"
    key.value = base64.b64encode(b"test-only-delegation-key-material").decode()
    store = AzureStore.__new__(AzureStore)
    store.account = "sampleaccount"
    store.blobs = SimpleNamespace(
        get_user_delegation_key=lambda *args: key,
        get_blob_client=lambda container, blob: SimpleNamespace(
            url=f"https://sampleaccount.blob.core.windows.net/{container}/{blob}",
        ),
    )
    job_id = str(uuid4())
    url = store.signed_url(job_id, upload=True)
    parsed = urlparse(url)
    query = parse_qs(parsed.query)
    assert parsed.path == f"/uploads/{job_id}.video"
    assert query["sp"] == ["c"]
    assert query["sr"] == ["b"]
    assert query["spr"] == ["https"]
    assert "sig" in query and "se" in query
    read = parse_qs(urlparse(store.signed_url(job_id, upload=False)).query)
    assert read["sp"] == ["r"]
