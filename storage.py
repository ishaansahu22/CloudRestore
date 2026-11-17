"""Amazon S3 operations used by CloudRestore.

This module talks to real S3 APIs. Tests inject mocked clients explicitly; the
application itself never substitutes simulated bucket data.
"""

from __future__ import annotations

import base64
import hashlib
import tempfile
from datetime import datetime, timezone
from typing import Any, BinaryIO

from botocore.exceptions import ClientError


class IntegrityUnavailable(RuntimeError):
    """The selected object version has no comparable full-object SHA-256."""


class S3Store:
    def __init__(self, client: Any, bucket: str, max_upload_bytes: int) -> None:
        self.client = client
        self.bucket = bucket
        self.max_upload_bytes = max_upload_bytes

    def require_versioning(self) -> str:
        status = self.client.get_bucket_versioning(Bucket=self.bucket).get("Status", "Disabled")
        if status != "Enabled":
            raise RuntimeError("Enable S3 bucket versioning before uploading or deleting files.")
        return status

    def _list_version_page(self, **kwargs: Any) -> dict[str, Any]:
        return self.client.list_object_versions(Bucket=self.bucket, MaxKeys=1000, **kwargs)

    @staticmethod
    def _entries(response: dict[str, Any]) -> list[dict[str, Any]]:
        rows = []
        for item in response.get("Versions", []):
            rows.append({
                "key": item["Key"], "version_id": item["VersionId"],
                "is_latest": bool(item.get("IsLatest", False)),
                "last_modified": item.get("LastModified"), "size": item.get("Size"),
                "delete_marker": False,
            })
        for item in response.get("DeleteMarkers", []):
            rows.append({
                "key": item["Key"], "version_id": item["VersionId"],
                "is_latest": bool(item.get("IsLatest", False)),
                "last_modified": item.get("LastModified"), "size": None,
                "delete_marker": True,
            })
        return rows

    def list_versions(self, key: str) -> tuple[list[dict[str, Any]], bool]:
        response = self._list_version_page(Prefix=key)
        rows = [row for row in self._entries(response) if row["key"] == key]
        rows.sort(key=lambda row: row["last_modified"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)
        return rows, bool(response.get("IsTruncated"))

    def dashboard(self, activity: list[dict[str, Any]]) -> dict[str, Any]:
        """Fetch a single real S3 version page and mark partial totals explicitly."""
        self.client.head_bucket(Bucket=self.bucket)
        versioning_response = self.client.get_bucket_versioning(Bucket=self.bucket)
        versioning = versioning_response.get("Status", "Disabled")

        encryption = "Unavailable"
        try:
            rules = self.client.get_bucket_encryption(Bucket=self.bucket)["ServerSideEncryptionConfiguration"]["Rules"]
            encryption = ", ".join(
                rule.get("ApplyServerSideEncryptionByDefault", {}).get("SSEAlgorithm", "Unknown")
                for rule in rules
            ) or "Unavailable"
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "ServerSideEncryptionConfigurationNotFoundError":
                # S3 automatically applies SSE-S3 to new objects, even without a
                # separately configured bucket-level encryption rule.
                encryption = "SSE-S3 default (bucket rule not returned)"

        public_access = "Unavailable"
        try:
            config = self.client.get_public_access_block(Bucket=self.bucket)["PublicAccessBlockConfiguration"]
            all_blocked = all(config.get(flag, False) for flag in (
                "BlockPublicAcls", "IgnorePublicAcls", "BlockPublicPolicy", "RestrictPublicBuckets"
            ))
            public_access = "All four bucket blocks enabled" if all_blocked else "Some bucket blocks disabled"
        except ClientError as exc:
            code = exc.response.get("Error", {}).get("Code", "")
            if code == "NoSuchPublicAccessBlockConfiguration":
                public_access = "No bucket-level setting returned; account-level block not checked"

        response = self._list_version_page()
        rows = self._entries(response)
        latest_by_key = {row["key"]: row for row in rows if row["is_latest"]}
        visible = [row for row in latest_by_key.values() if not row["delete_marker"]]
        versions = [row for row in rows if not row["delete_marker"]]
        recent_uploads = sorted(versions, key=lambda row: row["last_modified"] or datetime.min.replace(tzinfo=timezone.utc), reverse=True)[:8]
        files = []
        for key, latest in latest_by_key.items():
            key_versions = [row for row in rows if row["key"] == key and not row["delete_marker"]]
            files.append({
                "key": key,
                "last_modified": latest["last_modified"],
                "size": latest["size"],
                "deleted": latest["delete_marker"],
                "version_count": len(key_versions),
            })
        files.sort(key=lambda row: row["key"].lower())
        return {
            "connected": True,
            "region": self.client.meta.region_name,
            "bucket": self.bucket,
            "versioning": versioning,
            "encryption": encryption,
            "public_access": public_access,
            "objects": len(visible),
            "versions": len(versions),
            "delete_markers": sum(1 for row in rows if row["delete_marker"]),
            "recent_uploads": recent_uploads,
            "files": files,
            "recent_restores": [row for row in activity if row.get("action") == "restore"][:8],
            "activity": activity[:12],
            "truncated": bool(response.get("IsTruncated")),
        }

    def _spool_and_verify(self, key: str, version_id: str) -> tuple[BinaryIO, str, int, str]:
        response = self.client.get_object(
            Bucket=self.bucket, Key=key, VersionId=version_id, ChecksumMode="ENABLED"
        )
        spool = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
        digest = hashlib.sha256()
        size = 0
        body = response["Body"]
        try:
            for chunk in iter(lambda: body.read(64 * 1024), b""):
                digest.update(chunk)
                spool.write(chunk)
                size += len(chunk)
        finally:
            body.close()

        actual_hex = digest.hexdigest()
        metadata_digest = response.get("Metadata", {}).get("cloudrestore-sha256")
        aws_digest = response.get("ChecksumSHA256")
        checksum_type = response.get("ChecksumType")
        if metadata_digest:
            expected_matches = actual_hex.lower() == metadata_digest.lower()
        elif aws_digest and checksum_type != "COMPOSITE":
            expected_matches = base64.b64encode(digest.digest()).decode("ascii") == aws_digest
        else:
            spool.close()
            raise IntegrityUnavailable(
                "This S3 version has no comparable full-object SHA-256. It was not downloaded or restored."
            )
        if not expected_matches:
            spool.close()
            raise ValueError("SHA-256 verification failed; the object was not downloaded or restored.")
        spool.seek(0)
        return spool, actual_hex, size, response.get("ContentType", "application/octet-stream")

    def upload(self, key: str, source: BinaryIO, content_type: str) -> dict[str, Any]:
        self.require_versioning()
        spool = tempfile.SpooledTemporaryFile(max_size=1024 * 1024, mode="w+b")
        digest = hashlib.sha256()
        size = 0
        try:
            while True:
                chunk = source.read(64 * 1024)
                if not chunk:
                    break
                size += len(chunk)
                if size > self.max_upload_bytes:
                    raise ValueError("File exceeds the configured upload limit.")
                digest.update(chunk)
                spool.write(chunk)
            checksum = digest.digest()
            spool.seek(0)
            response = self.client.put_object(
                Bucket=self.bucket, Key=key, Body=spool, ContentLength=size,
                ContentType=content_type or "application/octet-stream",
                ServerSideEncryption="AES256",
                ChecksumAlgorithm="SHA256",
                ChecksumSHA256=base64.b64encode(checksum).decode("ascii"),
                Metadata={"cloudrestore-sha256": digest.hexdigest()},
            )
            if not response.get("VersionId"):
                raise RuntimeError("S3 did not return a VersionId. Confirm bucket versioning is enabled.")
            return {"version_id": response["VersionId"], "sha256": digest.hexdigest(), "size": size}
        finally:
            spool.close()

    def read_version(self, key: str, version_id: str) -> tuple[BinaryIO, str, int, str]:
        return self._spool_and_verify(key, version_id)

    def restore(self, key: str, version_id: str) -> dict[str, Any]:
        self.require_versioning()
        spool, digest, size, content_type = self._spool_and_verify(key, version_id)
        try:
            response = self.client.put_object(
                Bucket=self.bucket, Key=key, Body=spool, ContentLength=size,
                ContentType=content_type, ServerSideEncryption="AES256",
                ChecksumAlgorithm="SHA256", ChecksumSHA256=base64.b64encode(bytes.fromhex(digest)).decode("ascii"),
                Metadata={"cloudrestore-sha256": digest},
            )
        finally:
            spool.close()
        if not response.get("VersionId"):
            raise RuntimeError("S3 did not return a VersionId for the restored copy.")
        return {"version_id": response["VersionId"], "sha256": digest, "size": size}

    def delete(self, key: str) -> dict[str, Any]:
        self.require_versioning()
        return self.client.delete_object(Bucket=self.bucket, Key=key)

