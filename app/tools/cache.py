"""Disposable, versioned Redis cache. SQL and durable state always have local truth."""

import hashlib
import json
import time
from collections.abc import Callable
from typing import Any
from uuid import uuid4

from app.agent.reliability import cancellation
from app.domain.models import QueryData, ToolResult
from app.observability.context import emit
from app.settings import Settings

RELEASE = "if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('del',KEYS[1]) else return 0 end"


class QueryCache:
    def __init__(self, settings: Settings):
        self.settings = settings
        self.client: Any = None
        if settings.redis_url.get_secret_value():
            from redis import Redis

            self.client = Redis.from_url(
                settings.redis_url.get_secret_value(),
                decode_responses=True,
                socket_connect_timeout=settings.redis_timeout_seconds,
                socket_timeout=settings.redis_timeout_seconds,
                retry_on_timeout=False,
            )

    def key(self, kind: str, identity: str) -> str:
        manifest = self.settings.data_dir / "manifest.json"
        version = (
            hashlib.sha256(manifest.read_bytes()).hexdigest()[:16]
            if manifest.exists()
            else "missing"
        )
        return f"{self.settings.cache_namespace}:{self.settings.database_backend}:{version}:{kind}:{identity}"

    def mirror(self, kind: str, identity: str, value: dict[str, Any]) -> None:
        if self.client is None:
            return
        try:
            self.client.set(
                self.key(kind, identity), json.dumps(value), ex=self.settings.cache_ttl_seconds
            )
        except Exception:
            emit("cache_degraded", {"operation": "mirror", "reason": "redis_unavailable"})

    def get_or_compute(
        self, identity: str, compute: Callable[[], ToolResult[QueryData]]
    ) -> ToolResult[QueryData]:
        if self.client is None:
            return compute()
        key = self.key("sql", identity)
        lock, token = key + ":lock", uuid4().hex
        acquired = False
        try:
            # The lease exceeds bounded connection + query time. Waiters never hold a DB connection.
            lease = (
                self.settings.database_connect_timeout_seconds
                + self.settings.query_timeout_seconds
                + 5
            )
            deadline = time.monotonic() + lease
            while True:
                scope = cancellation.get()
                if scope:
                    scope.check()
                cached = self.client.get(key)
                if cached:
                    value = ToolResult[QueryData].model_validate_json(cached)
                    digest = identity.split(":", 1)[0]
                    if len(digest) == 64 and (not value.data or value.data.query_hash != digest):
                        raise ValueError("Cache query identity mismatch")
                    emit("cache", {"status": "hit", "query_hash": identity})
                    return value
                acquired = bool(self.client.set(lock, token, nx=True, px=int(lease * 1000)))
                if acquired:
                    break
                if time.monotonic() >= deadline:
                    emit("cache_degraded", {"reason": "lock_wait_timeout"})
                    break
                if scope:
                    scope.event.wait(0.025)
                else:
                    time.sleep(0.025)
        except Exception:
            scope = cancellation.get()
            if scope:
                scope.check()
            emit("cache_degraded", {"reason": "redis_unavailable_or_invalid_value"})
            return compute()
        if not acquired:
            return compute()
        try:
            value = compute()
            try:
                # Publish only while holding the original lease.
                self.client.eval(
                    "if redis.call('get',KEYS[1]) == ARGV[1] then return redis.call('set',KEYS[2],ARGV[2],'EX',ARGV[3]) else return 0 end",
                    2,
                    lock,
                    key,
                    token,
                    value.model_dump_json(),
                    self.settings.cache_ttl_seconds,
                )
                emit("cache", {"status": "miss", "query_hash": identity})
            except Exception:
                emit("cache_degraded", {"reason": "redis_write_failed"})
            return value
        finally:
            if acquired:
                try:
                    self.client.eval(RELEASE, 1, lock, token)
                except Exception:
                    emit("cache_degraded", {"reason": "redis_unlock_failed"})

    def close(self) -> None:
        if self.client is not None:
            self.client.close()
