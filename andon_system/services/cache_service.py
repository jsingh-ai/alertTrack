from __future__ import annotations

import hashlib
import json
import os
import threading
import time

from flask import current_app, has_app_context

try:
    import redis
except ImportError:  # pragma: no cover - optional dependency
    redis = None


_LOCAL_CACHE = {}
_LOCAL_VERSIONS = {}
_LOCAL_LOCK = threading.RLock()
_REDIS_CLIENT = None
_REDIS_DISABLED = False
_REDIS_LOCK = threading.RLock()

_CACHE_PREFIX = "andon:cache"
_VERSION_PREFIX = "andon:cachever"


def get_cached(key):
    cache_key = _build_cache_key(key)
    client = _get_redis_client()
    if client is not None:
        try:
            payload = client.get(cache_key)
        except _redis_transport_errors():
            _mark_redis_unavailable()
            if _redis_required():
                raise
            return _get_local_cached(cache_key)
        if not payload:
            return None
        try:
            data = json.loads(payload)
        except json.JSONDecodeError:
            _safe_redis_delete(client, cache_key)
            return None
        expires_at = float(data.get("expires_at") or 0)
        if expires_at and expires_at <= time.time():
            _safe_redis_delete(client, cache_key)
            return None
        return data.get("value")

    return _get_local_cached(cache_key)


def set_cached(key, value, ttl_seconds: int):
    ttl_seconds = max(1, int(ttl_seconds))
    cache_key = _build_cache_key(key)
    expires_at = time.time() + ttl_seconds
    payload = json.dumps(
        {
            "expires_at": expires_at,
            "value": value,
        },
        separators=(",", ":"),
        sort_keys=True,
    )

    client = _get_redis_client()
    if client is not None:
        try:
            client.setex(cache_key, ttl_seconds, payload)
            return
        except _redis_transport_errors():
            _mark_redis_unavailable()
            if _redis_required():
                raise

    with _LOCAL_LOCK:
        _LOCAL_CACHE[cache_key] = {
            "value": value,
            "expires_at": time.monotonic() + ttl_seconds,
        }


def invalidate_cache(namespace: str | None = None, company_id=None):
    if namespace is None and company_id is None:
        _bump_version("global")
        return
    if company_id is not None:
        _bump_version("company", company_id)
    if namespace is not None:
        _bump_version("namespace", company_id, namespace)


def _build_cache_key(key):
    namespace, company_id, logical_key = _scope_from_key(key)
    version_parts = [_get_version_token("global")]
    if company_id is not None:
        version_parts.append(_get_version_token("company", company_id))
    if namespace is not None:
        version_parts.append(_get_version_token("namespace", company_id, namespace))
    fingerprint = hashlib.sha1(
        json.dumps(_normalize(logical_key), separators=(",", ":"), sort_keys=True).encode("utf-8"),
    ).hexdigest()
    return f"{_CACHE_PREFIX}:{':'.join(version_parts)}:{fingerprint}"


def _scope_from_key(key):
    if isinstance(key, (tuple, list)) and key:
        namespace = key[0]
        company_id = key[1] if len(key) > 1 else None
        return namespace, company_id, key
    return None, None, key


def _normalize(value):
    if isinstance(value, dict):
        return {str(k): _normalize(v) for k, v in sorted(value.items(), key=lambda item: str(item[0]))}
    if isinstance(value, (list, tuple)):
        return [_normalize(item) for item in value]
    if isinstance(value, set):
        return sorted(_normalize(item) for item in value)
    return value


def _get_version_token(scope_type, company_id=None, namespace=None):
    client = _get_redis_client()
    if client is not None:
        version_key = _version_key(scope_type, company_id, namespace)
        try:
            version = client.get(version_key)
        except _redis_transport_errors():
            _mark_redis_unavailable()
            if _redis_required():
                raise
            with _LOCAL_LOCK:
                return str(_LOCAL_VERSIONS.get(_version_key(scope_type, company_id, namespace), 0))
        return version or "0"

    with _LOCAL_LOCK:
        return str(_LOCAL_VERSIONS.get(_version_key(scope_type, company_id, namespace), 0))


def _bump_version(scope_type, company_id=None, namespace=None):
    client = _get_redis_client()
    version_key = _version_key(scope_type, company_id, namespace)
    if client is not None:
        try:
            client.incr(version_key)
            return
        except _redis_transport_errors():
            _mark_redis_unavailable()
            if _redis_required():
                raise

    with _LOCAL_LOCK:
        _LOCAL_VERSIONS[version_key] = _LOCAL_VERSIONS.get(version_key, 0) + 1


def _version_key(scope_type, company_id=None, namespace=None):
    if scope_type == "global":
        return f"{_VERSION_PREFIX}:global"
    if scope_type == "company":
        return f"{_VERSION_PREFIX}:company:{company_id}"
    if scope_type == "namespace":
        return f"{_VERSION_PREFIX}:namespace:{company_id}:{namespace}"
    return f"{_VERSION_PREFIX}:unknown"


def _get_redis_client():
    global _REDIS_CLIENT
    global _REDIS_DISABLED
    if redis is None:
        return None
    url = os.getenv("REDIS_URL")
    if not url:
        return None
    if _REDIS_DISABLED and not _redis_required():
        return None
    with _REDIS_LOCK:
        if _REDIS_CLIENT is not None:
            return _REDIS_CLIENT
        try:
            _REDIS_CLIENT = redis.Redis.from_url(url, decode_responses=True)
            _REDIS_CLIENT.ping()
        except _redis_transport_errors():
            _REDIS_CLIENT = None
            _REDIS_DISABLED = not _redis_required()
            if _redis_required():
                raise
        return _REDIS_CLIENT


def _get_local_cached(cache_key):
    with _LOCAL_LOCK:
        entry = _LOCAL_CACHE.get(cache_key)
        if not entry:
            return None
        if entry["expires_at"] <= time.monotonic():
            _LOCAL_CACHE.pop(cache_key, None)
            return None
        return entry["value"]


def _safe_redis_delete(client, cache_key):
    try:
        client.delete(cache_key)
    except _redis_transport_errors():
        _mark_redis_unavailable()
        if _redis_required():
            raise


def _mark_redis_unavailable():
    global _REDIS_CLIENT
    global _REDIS_DISABLED
    with _REDIS_LOCK:
        _REDIS_CLIENT = None
        _REDIS_DISABLED = True


def _redis_required():
    if has_app_context():
        return bool(current_app.config.get("REDIS_REQUIRED"))
    return os.getenv("REDIS_REQUIRED", "false").lower() in {"1", "true", "yes", "on"}


def _redis_transport_errors():
    if redis is None:
        return (OSError,)
    errors = [OSError]
    for attr in ("ConnectionError", "TimeoutError", "BusyLoadingError", "ResponseError"):
        exc = getattr(redis.exceptions, attr, None)
        if exc is not None:
            errors.append(exc)
    return tuple(dict.fromkeys(errors))
