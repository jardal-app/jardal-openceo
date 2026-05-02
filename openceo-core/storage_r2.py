"""Cloudflare R2 storage module with orphan cleanup support."""
from __future__ import annotations

import logging
from typing import Any
from uuid import uuid4

import boto3
from botocore.config import Config as BotoConfig
from botocore.exceptions import ClientError

logger = logging.getLogger(__name__)


def _get_s3_client(settings: Any):
    """Build a boto3 S3 client pointing to Cloudflare R2."""
    return boto3.client(
        "s3",
        region_name="auto",
        endpoint_url=settings.R2_ENDPOINT_URL,
        aws_access_key_id=settings.R2_ACCESS_KEY_ID,
        aws_secret_access_key=settings.R2_SECRET_ACCESS_KEY,
        config=BotoConfig(
            retries={"max_attempts": 3, "mode": "standard"},
            connect_timeout=10,
            read_timeout=60,
        ),
    )


def _build_public_url(settings: Any, key: str) -> str:
    """Return a public URL if R2_PUBLIC_BASE_URL is set, otherwise a 7-day signed URL."""
    base = (settings.R2_PUBLIC_BASE_URL or "").rstrip("/")
    if base:
        return f"{base}/{key}"
    # Generate a 7-day signed URL so files remain accessible without auth
    return _generate_signed_url(settings, key, expires_in=604800)


def _generate_signed_url(settings: Any, key: str, expires_in: int = 604800) -> str:
    s3 = _get_s3_client(settings)
    return s3.generate_presigned_url(
        "get_object",
        Params={"Bucket": settings.R2_BUCKET_NAME, "Key": key},
        ExpiresIn=expires_in,
    )


def upload_file_sync(
    settings: Any,
    file_bytes: bytes,
    original_name: str,
    content_type: str | None = None,
) -> dict[str, Any]:
    """Synchronous upload to R2. Returns metadata dict on success."""
    s3 = _get_s3_client(settings)
    bucket = settings.R2_BUCKET_NAME
    key = f"{uuid4().hex}-{original_name}"

    extra_args: dict[str, Any] = {}
    if content_type:
        extra_args["ContentType"] = content_type

    s3.put_object(Bucket=bucket, Key=key, Body=file_bytes, **extra_args)
    url = _build_public_url(settings, key)

    return {
        "url": url,
        "key": key,
        "name": original_name,
        "type": content_type or "application/octet-stream",
    }


def delete_file_sync(settings: Any, key: str) -> bool:
    """Synchronous delete from R2. Returns True if deleted or already gone."""
    s3 = _get_s3_client(settings)
    bucket = settings.R2_BUCKET_NAME
    try:
        s3.delete_object(Bucket=bucket, Key=key)
        return True
    except ClientError as exc:
        error_code = exc.response.get("Error", {}).get("Code", "Unknown")
        if error_code == "NoSuchKey":
            return True
        logger.error("R2 delete failed for key %s: %s", key, exc)
        return False


def delete_files_sync(settings: Any, keys: list[str]) -> list[str]:
    """Delete multiple files. Returns list of keys that failed."""
    failed: list[str] = []
    for key in keys:
        if not delete_file_sync(settings, key):
            failed.append(key)
    return failed


def generate_presigned_url_sync(settings: Any, key: str, expires_in: int = 3600) -> str:
    """Generate a temporary signed URL for private objects."""
    return _generate_signed_url(settings, key, expires_in)
