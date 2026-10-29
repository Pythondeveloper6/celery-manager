"""Ownership-safe distributed leases. No process-local production fallback."""
from __future__ import annotations

import hashlib
import json
import math
import secrets
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from typing import Protocol


class SingletonUnavailable(RuntimeError):
    """The lock authority cannot safely authorize execution."""


class SingletonLeaseLost(RuntimeError):
    """Execution completed after ownership became uncertain; inspect side effects."""


class SingletonConflict(RuntimeError):
    pass


@dataclass(frozen=True)
class Lease:
    key: str
    lease_id: str
    task_id: str
    task_name: str
    acquired_at: str
    ttl_ms: int = 0


class LockProvider(Protocol):
    def acquire(self, key: str, task_id: str, task_name: str, ttl: float) -> tuple[bool, Lease]: ...
    def renew(self, lease: Lease, ttl: float) -> bool: ...
    def release(self, lease: Lease) -> bool: ...
    def inspect(self, key: str) -> Lease | None: ...
    def list(self, limit: int = 100) -> list[Lease]: ...


def validate_key(key):
    if not isinstance(key, str) or not key or len(key) > 512:
        raise ValueError("singleton key must contain 1 to 512 characters")
    return key


def ttl_ms(ttl):
    if isinstance(ttl, bool) or not isinstance(ttl, (int, float)) or not math.isfinite(ttl) or ttl < 1:
        raise ValueError("singleton_ttl must be a finite number of at least 1 second")
    return math.ceil(ttl * 1000)


_ACQUIRE = """
local value = redis.call('GET', KEYS[1])
if value then return {0, value, redis.call('PTTL', KEYS[1])} end
redis.call('SET', KEYS[1], ARGV[1], 'PX', ARGV[2])
return {1, ARGV[1], tonumber(ARGV[2])}
"""
_RENEW = """
local value = redis.call('GET', KEYS[1])
if not value or cjson.decode(value).lease_id ~= ARGV[1] then return 0 end
return redis.call('PEXPIRE', KEYS[1], ARGV[2])
"""
_RELEASE = """
local value = redis.call('GET', KEYS[1])
if not value or cjson.decode(value).lease_id ~= ARGV[1] then return 0 end
return redis.call('DEL', KEYS[1])
"""
_INSPECT = """
local value = redis.call('GET', KEYS[1])
if not value then return nil end
return {value, redis.call('PTTL', KEYS[1])}
"""


class RedisLockProvider:
    """Redis single-authority leases, safe across processes and threads.

    Use a dedicated non-evicting Redis authority. Asynchronous replication
    failover and eviction can lose leases; this is not a fencing protocol.
    """

    def __init__(self, url=None, *, client=None, prefix="celery-manager:singleton:"):
        if not isinstance(prefix, str) or not prefix or any(c in prefix for c in '*?[]\\'):
            raise ValueError("lock prefix must be nonempty and contain no Redis glob characters")
        if client is None:
            from redis import Redis
            client = Redis.from_url(url, socket_timeout=5, socket_connect_timeout=5)
        self.client, self.prefix = client, prefix

    def _key(self, key):
        return self.prefix + hashlib.sha256(validate_key(key).encode()).hexdigest()

    @staticmethod
    def _decode(value, ttl):
        return Lease(**{**json.loads(value), "ttl_ms": int(ttl)})

    def acquire(self, key, task_id, task_name, ttl):
        lease = Lease(validate_key(key), secrets.token_urlsafe(24), task_id, task_name,
                      datetime.now(timezone.utc).isoformat())
        result = self.client.eval(_ACQUIRE, 1, self._key(key), json.dumps(asdict(lease)), ttl_ms(ttl))
        return bool(result[0]), self._decode(result[1], result[2])

    def renew(self, lease, ttl):
        return bool(self.client.eval(_RENEW, 1, self._key(lease.key), lease.lease_id, ttl_ms(ttl)))

    def release(self, lease):
        return bool(self.client.eval(_RELEASE, 1, self._key(lease.key), lease.lease_id))

    def inspect(self, key):
        result = self.client.eval(_INSPECT, 1, self._key(key))
        return self._decode(*result) if result else None

    def list(self, limit=100):
        if not 1 <= limit <= 1000:
            raise ValueError("limit must be between 1 and 1000")
        rows, seen = [], set()
        # SCAN is deliberately bounded; a busy/huge namespace can return a
        # partial snapshot, and callers must not treat this as an inventory.
        cursor = 0
        prefix_bytes = self.prefix.encode()
        for _ in range(100):
            cursor, keys = self.client.scan(cursor, match=self.prefix + '?' * 64, count=100)
            for key in keys:
                # A provider's prefix may contain another provider's prefix.
                # Only inspect our exact prefix + SHA-256 key shape, never a
                # nested namespace or unrelated data sharing the same Redis.
                encoded = key.encode() if isinstance(key, str) else key
                suffix = encoded[len(prefix_bytes):]
                if (not encoded.startswith(prefix_bytes) or len(suffix) != 64
                        or any(char not in b"0123456789abcdef" for char in suffix)):
                    continue
                if key in seen:
                    continue
                seen.add(key)
                result = self.client.eval(_INSPECT, 1, key)
                if result:
                    rows.append(self._decode(*result))
                    if len(rows) >= limit:
                        return rows
            if not cursor:
                break
        return rows


def provider_for(config):
    if config is None:
        raise SingletonUnavailable("singleton execution requires celery_manager.setup(config)")
    provider = getattr(config, 'lock_provider', None)
    if provider is not None:
        return provider
    cached = getattr(config, '_cm_lock_provider', None)
    if cached is not None:
        return cached
    url = getattr(getattr(config, 'celery_app', None), 'conf', {}).get('broker_url')
    if not isinstance(url, str) or not url.startswith(('redis://', 'rediss://', 'unix://')):
        raise SingletonUnavailable("Configure lock_provider=RedisLockProvider(...) for a non-Redis broker")
    provider = RedisLockProvider(url)
    config._cm_lock_provider = provider
    return provider
