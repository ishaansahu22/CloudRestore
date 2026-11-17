"""CloudRestore Flask application backed by actual Amazon S3 APIs."""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

import boto3
from botocore.exceptions import ClientError, NoCredentialsError, ProfileNotFound
from dotenv import load_dotenv
from flask import Flask, abort, flash, redirect, render_template, request, send_file, url_for
from werkzeug.utils import secure_filename

from storage import IntegrityUnavailable, S3Store

load_dotenv()


def describe_error(exc: Exception) -> str:
    """Return actionable errors without including credentials or raw SDK data."""
    if isinstance(exc, ProfileNotFound):
        return "AWS CLI profile not found. Configure the cloudrestore profile with `aws configure --profile cloudrestore`."
    if isinstance(exc, NoCredentialsError):
        return "AWS credentials were not found. Configure the AWS CLI profile or the standard AWS credential provider."
    if isinstance(exc, IntegrityUnavailable):
        return str(exc)
    if isinstance(exc, ValueError):
        return str(exc)
    if isinstance(exc, ClientError):
        code = exc.response.get("Error", {}).get("Code", "AWS service error")
        if code in {"AccessDenied", "AccessDeniedException", "403"}:
            return "AWS denied this operation. Check the IAM permissions for the configured profile and bucket."
        if code in {"NoSuchBucket", "404", "NotFound"}:
            return "The configured S3 bucket was not found in this region. Check the bucket name and AWS_REGION."
        if code in {"PermanentRedirect", "AuthorizationHeaderMalformed", "IncorrectEndpoint"}:
            return "The bucket region does not match AWS_REGION. Set AWS_REGION to the bucket's region."
        if code in {"NoSuchKey", "NoSuchVersion", "InvalidArgument"}:
            return "That object or version is no longer available. Refresh the page and select an existing version."
        return f"AWS returned {code}. Check the bucket, region, and IAM permissions."
    if isinstance(exc, (OSError, TimeoutError, ConnectionError)):
        return "Could not reach AWS. Check your network connection and try again."
    return "The operation failed. Check the application terminal for the error category."


def create_app(
    client_factory: Callable[[str, str | None], Any] | None = None,
    activity_path: str | Path | None = None,
) -> Flask:
    app = Flask(__name__)
    app.config["SECRET_KEY"] = os.environ.get("FLASK_SECRET_KEY", "local-development-only-change-before-hosting")
    aws_region = os.environ.get("AWS_REGION", "ap-southeast-2").strip()
    aws_profile = os.environ.get("AWS_PROFILE", "cloudrestore").strip() or None
    bucket_name = os.environ.get("AWS_S3_BUCKET", "").strip()
    max_upload_bytes = int(os.environ.get("MAX_UPLOAD_MB", "10")) * 1024 * 1024
    app.config["MAX_CONTENT_LENGTH"] = max_upload_bytes + 1024 * 1024  # multipart overhead
    journal_path = Path(activity_path or os.environ.get("ACTIVITY_LOG_PATH", "instance/activity.jsonl"))
    if not journal_path.is_absolute():
        journal_path = Path(app.root_path) / journal_path

    def make_client() -> Any:
        if client_factory:
            return client_factory(aws_region, aws_profile)
        session = boto3.Session(profile_name=aws_profile, region_name=aws_region)
        return session.client("s3")

    def store() -> S3Store:
        if not bucket_name:
            raise RuntimeError("Set AWS_S3_BUCKET in .env before starting CloudRestore.")
        return S3Store(make_client(), bucket_name, max_upload_bytes)

    def read_activity() -> list[dict[str, Any]]:
        try:
            with journal_path.open("r", encoding="utf-8") as journal:
                lines = journal.readlines()[-100:]
            return [json.loads(line) for line in reversed(lines) if line.strip()]
        except FileNotFoundError:
            return []
        except (OSError, json.JSONDecodeError):
            app.logger.warning("Activity journal could not be read")
            return []

    def log_activity(action: str, key: str, **details: Any) -> None:
        record = {
            "time": datetime.now(timezone.utc).isoformat(),
            "action": action,
            "key": key,
            **details,
        }
        journal_path.parent.mkdir(parents=True, exist_ok=True)
        with journal_path.open("a", encoding="utf-8") as journal:
            journal.write(json.dumps(record, separators=(",", ":")) + "\n")

    def clean_key(filename: str) -> str:
        key = secure_filename(filename or "")
        if not key or key in {".", ".."}:
            abort(400, "Choose a valid filename.")
        return key[:180]

    def clean_version_id(version_id: str) -> str:
        # Version IDs are opaque AWS values. Do not assume their character set;
        # only reject empty, oversized, or control-character values.
        if not version_id or len(version_id) > 1024 or any(ord(ch) < 32 for ch in version_id):
            abort(400, "Invalid S3 version ID.")
        return version_id

    @app.get("/")
    def index() -> Any:
        try:
            dashboard = store().dashboard(read_activity())
            return render_template("index.html", dashboard=dashboard, ready=True,
                                   setup_error=None, max_upload_mb=max_upload_bytes // (1024 * 1024))
        except Exception as exc:
            message = describe_error(exc)
            app.logger.warning("AWS dashboard request failed: %s", type(exc).__name__)
            return render_template(
                "index.html", dashboard={
                    "connected": False, "region": aws_region,
                    "bucket": bucket_name or "not configured",
                    "versioning": "Unavailable", "encryption": "Unavailable",
                    "public_access": "Unavailable", "objects": None,
                    "versions": None, "delete_markers": None,
                    "recent_uploads": [], "recent_restores": [],
                    "activity": read_activity(), "truncated": False,
                }, ready=False, setup_error=message,
                max_upload_mb=max_upload_bytes // (1024 * 1024),
            ), 503

    @app.post("/upload")
    def upload() -> Any:
        uploaded = request.files.get("file")
        if not uploaded or not uploaded.filename:
            flash("Choose a file to upload.", "error")
            return redirect(url_for("index"))
        key = clean_key(uploaded.filename)
        try:
            result = store().upload(key, uploaded.stream, uploaded.mimetype)
            log_activity("upload", key, version_id=result["version_id"],
                         sha256=result["sha256"], size=result["size"])
            flash(f"Uploaded {key} as S3 version {result['version_id']}.", "success")
        except Exception as exc:
            message = describe_error(exc)
            app.logger.warning("S3 upload failed: %s", type(exc).__name__)
            flash(message, "error")
        return redirect(url_for("index"))

    @app.get("/files/<path:key>/versions")
    def versions(key: str) -> Any:
        key = clean_key(key)
        try:
            rows, truncated = store().list_versions(key)
            return render_template("versions.html", key=key, versions=rows,
                                   truncated=truncated, bucket=bucket_name)
        except Exception as exc:
            app.logger.warning("S3 version listing failed: %s", type(exc).__name__)
            flash(describe_error(exc), "error")
            return redirect(url_for("index"))

    @app.post("/files/<path:key>/restore/<version_id>")
    def restore(key: str, version_id: str) -> Any:
        key = clean_key(key)
        version_id = clean_version_id(version_id)
        try:
            result = store().restore(key, version_id)
            # Log only after S3 returned the new version ID successfully.
            log_activity("restore", key, source_version_id=version_id,
                         version_id=result["version_id"], sha256=result["sha256"], size=result["size"])
            flash(f"Restored and SHA-256 verified {key} as new S3 version {result['version_id']}.", "success")
        except Exception as exc:
            app.logger.warning("S3 restore failed: %s", type(exc).__name__)
            flash(describe_error(exc), "error")
        return redirect(url_for("versions", key=key))

    @app.post("/files/<path:key>/delete")
    def delete(key: str) -> Any:
        key = clean_key(key)
        if request.form.get("confirm") != "yes":
            abort(400, "Confirm the delete action.")
        try:
            response = store().delete(key)
            log_activity("delete", key, version_id=response.get("VersionId"),
                         delete_marker=bool(response.get("DeleteMarker", False)))
            flash("S3 added a delete marker. Existing object versions remain available for recovery.", "success")
        except Exception as exc:
            app.logger.warning("S3 delete failed: %s", type(exc).__name__)
            flash(describe_error(exc), "error")
        return redirect(url_for("index"))

    @app.get("/files/<path:key>/download/<version_id>")
    def download(key: str, version_id: str) -> Any:
        key = clean_key(key)
        version_id = clean_version_id(version_id)
        spool = None
        try:
            # S3Store does integrity verification and spools content to disk when needed.
            spool, digest, size, content_type = store().read_version(key, version_id)
            log_activity("download", key, version_id=version_id, sha256=digest, size=size)
            result = send_file(spool, as_attachment=True, download_name=key,
                               mimetype=content_type,
                               conditional=False, etag=False)
            result.headers["X-CloudRestore-Integrity"] = "SHA-256 verified"
            result.headers["X-CloudRestore-SHA256"] = digest
            result.call_on_close(spool.close)
            return result
        except Exception as exc:
            if spool:
                spool.close()
            app.logger.warning("S3 download failed: %s", type(exc).__name__)
            flash(describe_error(exc), "error")
            return redirect(url_for("versions", key=key))

    @app.errorhandler(413)
    def too_large(_: Any) -> tuple[str, int]:
        return "File is too large. Reduce its size and try again.", 413

    return app


app = create_app()

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO)
    app.run(host="127.0.0.1", port=int(os.environ.get("PORT", "5000")), debug=False)

