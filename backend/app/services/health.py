"""Read-only dependency checks. Never return connection strings or exception text."""

import asyncio
from typing import Protocol

import boto3
import httpx
from botocore.config import Config
from redis.asyncio import Redis
from sqlalchemy import create_engine, text

from app.core.config import Settings


class DependencyProbe(Protocol):
    async def check(self) -> dict[str, bool]: ...


class InfrastructureProbe:
    def __init__(self, settings: Settings) -> None:
        self.settings = settings

    def _postgres(self) -> bool:
        if not self.settings.database_url.get_secret_value():
            return False
        engine = create_engine(
            self.settings.database_url.get_secret_value(),
            connect_args={"connect_timeout": max(1, int(self.settings.dependency_timeout_seconds))},
        )
        try:
            with engine.connect() as connection:
                return connection.execute(text("SELECT 1")).scalar() == 1
        finally:
            engine.dispose()

    def _storage(self) -> bool:
        if not self.settings.s3_access_key.get_secret_value():
            return False
        client = boto3.client(
            "s3",
            endpoint_url=self.settings.s3_endpoint,
            region_name=self.settings.s3_region,
            aws_access_key_id=self.settings.s3_access_key.get_secret_value(),
            aws_secret_access_key=self.settings.s3_secret_key.get_secret_value(),
            config=Config(
                connect_timeout=self.settings.dependency_timeout_seconds,
                read_timeout=self.settings.dependency_timeout_seconds,
                retries={"max_attempts": 0},
            ),
        )
        try:
            client.head_bucket(Bucket=self.settings.s3_bucket)
            return True
        finally:
            client.close()

    async def _redis(self) -> bool:
        if not self.settings.redis_url.get_secret_value():
            return False
        client = Redis.from_url(
            self.settings.redis_url.get_secret_value(),
            socket_connect_timeout=self.settings.dependency_timeout_seconds,
            socket_timeout=self.settings.dependency_timeout_seconds,
        )
        try:
            return bool(await client.ping())
        finally:
            await client.aclose()

    async def _qdrant(self) -> bool:
        headers = {"api-key": self.settings.qdrant_api_key.get_secret_value()}
        async with httpx.AsyncClient(timeout=self.settings.dependency_timeout_seconds) as client:
            response = await client.get(f"{self.settings.qdrant_url}/collections", headers=headers)
            return response.status_code == 200

    async def check(self) -> dict[str, bool]:
        checks = [
            asyncio.to_thread(self._postgres),
            self._redis(),
            self._qdrant(),
            asyncio.to_thread(self._storage),
        ]
        results = await asyncio.gather(*checks, return_exceptions=True)
        return dict(
            zip(
                ("postgres", "redis", "qdrant", "object_storage"),
                (result is True for result in results),
                strict=True,
            )
        )
