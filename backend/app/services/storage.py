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
    def stat(self, key: str, version_id: str | None = None) -> ObjectStat: ...
    def exists(self, key: str, version_id: str | None = None) -> bool: ...
    def delete(self, key: str) -> None: ...
    def open(self, key: str, version_id: str) -> Any: ...
    def generate_read_reference(self, document_id: UUID, version_id: UUID) -> str: ...


def object_key(document_id: UUID, version_id: UUID) -> str:
    return f"documents/{document_id}/{version_id}/original/source.pdf"


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

    def open(self, key: str, version_id: str) -> Any:
        return self.client.get_object(Bucket=self.bucket, Key=key, VersionId=version_id)["Body"]

    def generate_read_reference(self, document_id: UUID, version_id: UUID) -> str:
        return f"/api/v1/documents/{document_id}/versions/{version_id}/source"
