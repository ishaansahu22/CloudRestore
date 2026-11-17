"""Mocked unit tests only: no AWS account or S3 requests are used here."""

import base64
import hashlib
import io
from datetime import datetime, timezone
from unittest.mock import MagicMock

import pytest
from botocore.exceptions import ClientError

from storage import IntegrityUnavailable, S3Store


def store_with_client():
    client = MagicMock()
    client.meta.region_name = "ap-southeast-2"
    return S3Store(client, "cloudrestore-test-bucket", 1024 * 1024), client


def object_response(data: bytes, key="demo.txt", version="v-old", checksum_metadata=True):
    return {
        "Body": io.BytesIO(data),
        "ContentType": "text/plain",
        "Metadata": ({"cloudrestore-sha256": hashlib.sha256(data).hexdigest()}
                     if checksum_metadata else {}),
        "ChecksumSHA256": base64.b64encode(hashlib.sha256(data).digest()).decode("ascii"),
        "ChecksumType": "FULL_OBJECT",
        "VersionId": version,
        "Key": key,
    }


def test_connection_bucket_access_and_real_dashboard_data():
    store, client = store_with_client()
    client.get_bucket_versioning.return_value = {"Status": "Enabled"}
    client.get_bucket_encryption.return_value = {"ServerSideEncryptionConfiguration": {"Rules": [
        {"ApplyServerSideEncryptionByDefault": {"SSEAlgorithm": "AES256"}}
    ]}}
    client.get_public_access_block.return_value = {"PublicAccessBlockConfiguration": {
        "BlockPublicAcls": True, "IgnorePublicAcls": True,
        "BlockPublicPolicy": True, "RestrictPublicBuckets": True,
    }}
    client.list_object_versions.return_value = {
        "Versions": [{"Key": "demo.txt", "VersionId": "v1", "IsLatest": True,
                       "LastModified": datetime.now(timezone.utc), "Size": 7}],
        "DeleteMarkers": [], "IsTruncated": False,
    }
    result = store.dashboard([])
    client.head_bucket.assert_called_once_with(Bucket=store.bucket)
    assert result["connected"] is True
    assert result["objects"] == 1
    assert result["versions"] == 1
    assert result["encryption"] == "AES256"
    assert result["public_access"] == "All four bucket blocks enabled"


def test_bucket_missing_or_forbidden_is_an_aws_error():
    store, client = store_with_client()
    client.head_bucket.side_effect = ClientError(
        {"Error": {"Code": "AccessDenied", "Message": "denied"}}, "HeadBucket"
    )
    with pytest.raises(ClientError):
        store.dashboard([])


def test_versioning_status_is_checked_and_disabled_is_rejected():
    store, client = store_with_client()
    client.get_bucket_versioning.return_value = {"Status": "Enabled"}
    assert store.require_versioning() == "Enabled"
    client.get_bucket_versioning.return_value = {"Status": "Suspended"}
    with pytest.raises(RuntimeError, match="Enable S3 bucket versioning"):
        store.require_versioning()


def test_first_upload_returns_actual_s3_version_and_sha256():
    store, client = store_with_client()
    client.get_bucket_versioning.return_value = {"Status": "Enabled"}
    client.put_object.return_value = {"VersionId": "aws-version-001"}
    result = store.upload("demo.txt", io.BytesIO(b"first"), "text/plain")
    assert result["version_id"] == "aws-version-001"
    assert result["sha256"] == hashlib.sha256(b"first").hexdigest()
    call = client.put_object.call_args.kwargs
    assert call["ChecksumAlgorithm"] == "SHA256"
    assert call["Metadata"]["cloudrestore-sha256"] == result["sha256"]
    assert call["ServerSideEncryption"] == "AES256"


def test_second_same_name_upload_creates_another_s3_version():
    store, client = store_with_client()
    client.get_bucket_versioning.return_value = {"Status": "Enabled"}
    client.put_object.side_effect = [{"VersionId": "aws-version-001"}, {"VersionId": "aws-version-002"}]
    first = store.upload("demo.txt", io.BytesIO(b"first"), "text/plain")
    second = store.upload("demo.txt", io.BytesIO(b"second"), "text/plain")
    assert first["version_id"] != second["version_id"]
    assert client.put_object.call_count == 2


def test_lists_real_versions_and_delete_markers():
    store, client = store_with_client()
    client.list_object_versions.return_value = {
        "Versions": [{"Key": "demo.txt", "VersionId": "v2", "IsLatest": True,
                       "LastModified": datetime.now(timezone.utc), "Size": 6}],
        "DeleteMarkers": [{"Key": "demo.txt", "VersionId": "marker1", "IsLatest": False,
                            "LastModified": datetime.now(timezone.utc)}],
        "IsTruncated": False,
    }
    rows, truncated = store.list_versions("demo.txt")
    assert {r["version_id"] for r in rows} == {"v2", "marker1"}
    assert next(r for r in rows if r["version_id"] == "marker1")["delete_marker"] is True
    assert truncated is False


def test_downloads_an_older_version_and_verifies_sha256():
    store, client = store_with_client()
    client.get_object.return_value = object_response(b"older file")
    spool, digest, size, content_type = store.read_version("demo.txt", "v-old")
    try:
        assert spool.read() == b"older file"
        assert digest == hashlib.sha256(b"older file").hexdigest()
        assert size == len(b"older file")
        assert content_type == "text/plain"
    finally:
        spool.close()
    client.get_object.assert_called_once_with(Bucket=store.bucket, Key="demo.txt",
                                              VersionId="v-old", ChecksumMode="ENABLED")


def test_restore_uploads_verified_content_as_a_new_version():
    store, client = store_with_client()
    client.get_bucket_versioning.return_value = {"Status": "Enabled"}
    client.get_object.return_value = object_response(b"restore me")
    uploaded = {}
    def capture_put(**kwargs):
        uploaded["body"] = kwargs["Body"].read()
        uploaded.update(kwargs)
        return {"VersionId": "restored-new-version"}
    client.put_object.side_effect = capture_put
    result = store.restore("demo.txt", "v-old")
    assert result["version_id"] == "restored-new-version"
    assert uploaded["body"] == b"restore me"
    assert uploaded["Metadata"]["cloudrestore-sha256"] == hashlib.sha256(b"restore me").hexdigest()


def test_sha256_mismatch_blocks_download_or_restore():
    store, client = store_with_client()
    response = object_response(b"tampered")
    response["Metadata"]["cloudrestore-sha256"] = hashlib.sha256(b"expected").hexdigest()
    client.get_object.return_value = response
    with pytest.raises(ValueError, match="SHA-256 verification failed"):
        store.read_version("demo.txt", "v-old")


def test_missing_comparable_checksum_is_not_claimed_as_verified():
    store, client = store_with_client()
    response = object_response(b"third-party", checksum_metadata=False)
    response["ChecksumType"] = "COMPOSITE"
    client.get_object.return_value = response
    with pytest.raises(IntegrityUnavailable):
        store.read_version("demo.txt", "v-old")


def test_delete_creates_s3_delete_marker_without_permanently_removing_versions():
    store, client = store_with_client()
    client.get_bucket_versioning.return_value = {"Status": "Enabled"}
    client.delete_object.return_value = {"DeleteMarker": True, "VersionId": "marker-1"}
    result = store.delete("demo.txt")
    assert result["DeleteMarker"] is True
    client.delete_object.assert_called_once_with(Bucket=store.bucket, Key="demo.txt")
    assert "delete_object_version" not in str(client.method_calls).lower()


def test_recovery_after_delete_marker_writes_a_new_latest_version():
    store, client = store_with_client()
    client.get_bucket_versioning.return_value = {"Status": "Enabled"}
    client.get_object.return_value = object_response(b"recover me", version="v-before-delete")
    client.put_object.return_value = {"VersionId": "v-recovered"}
    restored = store.restore("demo.txt", "v-before-delete")
    assert restored["version_id"] == "v-recovered"
    assert client.put_object.call_args.kwargs["Key"] == "demo.txt"


def test_auth_and_network_errors_are_not_reported_as_success():
    from app import describe_error

    denied = ClientError({"Error": {"Code": "AccessDenied", "Message": "denied"}}, "PutObject")
    assert "IAM permissions" in describe_error(denied)
    assert "network connection" in describe_error(ConnectionError("offline"))

