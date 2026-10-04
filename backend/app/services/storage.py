import hashlib
from dataclasses import dataclass
from typing import Any, BinaryIO, Protocol
from uuid import UUID

import boto3
from boto3.s3.transfer import TransferConfig
from botocore.config import Config
from botocore.exceptions import ClientError

from app.core.config import Settings


@dataclass(frozen=True)
class ObjectStat:
    size: int
    sha256: str
    version_id: str


class ObjectStorage(Protocol):
    def put(self, key: str, stream: BinaryIO, sha256: str) -> ObjectStat: ...
    def put_bytes(self, key: str, payload: bytes, content_type: str) -> ObjectStat: ...
    def stat(self, key: str, version_id: str | None = None) -> ObjectStat: ...
    def exists(self, key: str, version_id: str | None = None) -> bool: ...
    def delete(self, key: str) -> None: ...
    def delete_prefix(self, prefix: str) -> int: ...
    def open(self, key: str, version_id: str) -> Any: ...
    def read(self, key: str, version_id: str | None = None) -> bytes: ...
    def download(self, key: str, version_id: str, destination: Any) -> None: ...
    def generate_read_reference(self, document_id: UUID, version_id: UUID) -> str: ...


def object_key(document_id: UUID, version_id: UUID) -> str:
    return f"documents/{document_id}/{version_id}/original/source.pdf"


def parse_prefix(document_id: UUID, version_id: UUID, run_id: UUID) -> str:
    """All artifacts of one ParseRun live under a single immutable, UUID-addressed prefix."""
    return f"documents/{document_id}/{version_id}/parsing/{run_id}"


def document_prefix(document_id: UUID) -> str:
    """Everything one document owns in object storage: its originals and every parse artifact.

    The trailing slash is load-bearing. Without it a prefix listing would also match any other key
    that merely begins with the same characters.
    """
    return f"documents/{document_id}/"


def raw_parse_key(document_id: UUID, version_id: UUID, run_id: UUID) -> str:
    return f"{parse_prefix(document_id, version_id, run_id)}/docling.json"


def figure_key(
    document_id: UUID, version_id: UUID, run_id: UUID, figure_id: UUID, extension: str
) -> str:
    return f"{parse_prefix(document_id, version_id, run_id)}/figures/{figure_id}.{extension}"


def page_preview_key(
    document_id: UUID, version_id: UUID, run_id: UUID, page_number: int, extension: str
) -> str:
    return (
        f"{parse_prefix(document_id, version_id, run_id)}/pages/page-{page_number:04d}.{extension}"
    )


class S3ObjectStorage:
    def __init__(self, settings: Settings) -> None:
        self.bucket = settings.s3_bucket
        self.client = boto3.client(
            "s3",
            endpoint_url=settings.s3_endpoint,
            region_name=settings.s3_region,
            aws_access_key_id=settings.s3_access_key.get_secret_value(),
            aws_secret_access_key=settings.s3_secret_key.get_secret_value(),
            config=Config(
                signature_version="s3v4",
                connect_timeout=settings.ingestion.storage_timeout_seconds,
                read_timeout=settings.ingestion.storage_timeout_seconds,
                retries={"max_attempts": 1},
                s3={"addressing_style": "path"},
            ),
        )

    def put(self, key: str, stream: BinaryIO, sha256: str) -> ObjectStat:
        # Callers own a unique, durable intent and row lock for this never-reused key.
        stream.seek(0)
        self.client.upload_fileobj(
            stream,
            self.bucket,
            key,
            ExtraArgs={"ContentType": "application/pdf", "Metadata": {"sha256": sha256}},
            Config=TransferConfig(
                multipart_threshold=8 * 1024 * 1024,
                multipart_chunksize=8 * 1024 * 1024,
                use_threads=False,
            ),
        )
        return self.stat(key)

    def put_bytes(self, key: str, payload: bytes, content_type: str) -> ObjectStat:
        """Derived parse artifacts: immutable per ParseRun, addressed by that run's UUID."""
        digest = hashlib.sha256(payload).hexdigest()
        self.client.put_object(
            Bucket=self.bucket,
            Key=key,
            Body=payload,
            ContentType=content_type,
            Metadata={"sha256": digest},
        )
        return self.stat(key)

    def stat(self, key: str, version_id: str | None = None) -> ObjectStat:
        args = {"Bucket": self.bucket, "Key": key}
        if version_id:
            args["VersionId"] = version_id
        response = self.client.head_object(**args)
        version = response.get("VersionId")
        if not version or version == "null":
            raise RuntimeError("Versioned object storage is required")
        return ObjectStat(
            int(response["ContentLength"]),
            str(response.get("Metadata", {}).get("sha256", "")),
            str(version),
        )

    def exists(self, key: str, version_id: str | None = None) -> bool:
        try:
            self.stat(key, version_id)
            return True
        except ClientError as exc:
            if exc.response["Error"]["Code"] in {"404", "NoSuchKey", "NoSuchVersion"}:
                return False
            raise

    def delete(self, key: str) -> None:
        # Only for a locked, uncommitted intent. Remove actual versions, not just a delete marker.
        for page in self.client.get_paginator("list_object_versions").paginate(
            Bucket=self.bucket, Prefix=key
        ):
            versions = [
                {"Key": item["Key"], "VersionId": item["VersionId"]}
                for field in ("Versions", "DeleteMarkers")
                for item in page.get(field, [])
                if item["Key"] == key
            ]
            if versions:
                result = self.client.delete_objects(
                    Bucket=self.bucket, Delete={"Objects": versions, "Quiet": True}
                )
                if result.get("Errors"):
                    raise RuntimeError("Object compensation incomplete")
        for page in self.client.get_paginator("list_multipart_uploads").paginate(
            Bucket=self.bucket, Prefix=key
        ):
            for item in page.get("Uploads", []):
                if item["Key"] == key:
                    self.client.abort_multipart_upload(
                        Bucket=self.bucket, Key=key, UploadId=item["UploadId"]
                    )

    def delete_prefix(self, prefix: str) -> int:
        """Permanently remove every object version and delete marker under ``prefix``.

        The bucket is versioned, so an ordinary delete only adds a delete marker and leaves the
        bytes recoverable. Permanent deletion has to remove each version by id. Re-running it is
        harmless: an empty listing deletes nothing and returns 0.
        """
        if not prefix.endswith("/") or prefix.count("/") < 2:
            # A short or unterminated prefix could reach another document's objects.
            raise ValueError("Refusing an unscoped prefix delete")
        removed = 0
        for page in self.client.get_paginator("list_object_versions").paginate(
            Bucket=self.bucket, Prefix=prefix
        ):
            versions = [
                {"Key": item["Key"], "VersionId": item["VersionId"]}
                for field in ("Versions", "DeleteMarkers")
                for item in page.get(field, [])
                if item["Key"].startswith(prefix)
            ]
            # DeleteObjects accepts at most 1,000 keys per request; a listing page never exceeds
            # that, but versions and markers are combined, so batch defensively.
            for start in range(0, len(versions), 1000):
                batch = versions[start : start + 1000]
                result = self.client.delete_objects(
                    Bucket=self.bucket, Delete={"Objects": batch, "Quiet": True}
                )
                if result.get("Errors"):
                    raise RuntimeError("Object deletion incomplete")
                removed += len(batch)
        for page in self.client.get_paginator("list_multipart_uploads").paginate(
            Bucket=self.bucket, Prefix=prefix
        ):
            for item in page.get("Uploads", []):
                self.client.abort_multipart_upload(
                    Bucket=self.bucket, Key=item["Key"], UploadId=item["UploadId"]
                )
        return removed

    def open(self, key: str, version_id: str) -> Any:
        return self.client.get_object(Bucket=self.bucket, Key=key, VersionId=version_id)["Body"]

    def read(self, key: str, version_id: str | None = None) -> bytes:
        args = {"Bucket": self.bucket, "Key": key}
        if version_id:
            args["VersionId"] = version_id
        body = self.client.get_object(**args)["Body"]
        try:
            return bytes(body.read())
        finally:
            body.close()

    def download(self, key: str, version_id: str, destination: Any) -> None:
        """Stream an original to a local file handle without buffering it in worker memory."""
        self.client.download_fileobj(
            self.bucket,
            key,
            destination,
            ExtraArgs={"VersionId": version_id},
            Config=TransferConfig(use_threads=False),
        )

    def generate_read_reference(self, document_id: UUID, version_id: UUID) -> str:
        return f"/api/v1/documents/{document_id}/versions/{version_id}/source"
