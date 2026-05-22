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
_LOCAL_CACHE_MAX_ENTRIES = 2048
_LOCAL_VERSION_MAX_ENTRIES = 512
_LIVE_ALERT_NAMESPACES = ("active_alerts_list", "operator_snapshot", "board_state", "pager_active_alerts")


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
        _prune_local_cache_locked()
        _LOCAL_CACHE[cache_key] = {
            "value": value,
            "expires_at": time.monotonic() + ttl_seconds,
        }


def cache_runtime_status() -> dict:
    redis_url = os.getenv("REDIS_URL")
    configured = bool(redis_url)
    required = _redis_required()
    client = _get_redis_client()
    reachable = client is not None if configured else None
    backend = "redis" if client is not None else "memory"
    return {
        "backend": backend,
        "redis_configured": configured,
        "redis_required": required,
        "redis_reachable": reachable,
    }


def invalidate_cache(namespace: str | None = None, company_id=None):
    if namespace is None and company_id is None:
        _bump_version("global")
        return
    if company_id is not None:
        _bump_version("company", company_id)
    if namespace is not None:
        _bump_version("namespace", company_id, namespace)


def invalidate_live_alert_caches(company_id) -> dict:
    started_at = time.perf_counter()
    metrics = {
        "namespace": "live_alerts",
        "mode": "local",
        "lock_wait_ms": 0.0,
        "per_company_invalidate_ms": 0.0,
        "per_namespace_invalidate_ms": 0.0,
        "version_bump_ms": 0.0,
        "redis_incr_ms": 0.0,
        "redis_get_ms": 0.0,
        "redis_set_ms": 0.0,
        "redis_delete_ms": 0.0,
        "scan_ms": 0.0,
        "delete_ms": 0.0,
        "local_cache_clear_ms": 0.0,
        "local_prune_ms": 0.0,
    }
    company_started_at = time.perf_counter()
    _bump_version("company", company_id, _perf=metrics, prefer_local_fast=True)
    metrics["per_company_invalidate_ms"] = (time.perf_counter() - company_started_at) * 1000
    namespace_started_at = time.perf_counter()
    for ns in _LIVE_ALERT_NAMESPACES:
        _bump_version("namespace", company_id, ns, _perf=metrics, prefer_local_fast=True)
    metrics["per_namespace_invalidate_ms"] = (time.perf_counter() - namespace_started_at) * 1000
    metrics["total_ms"] = (time.perf_counter() - started_at) * 1000
    if has_app_context() and current_app.config.get("ANDON_PERF_LOGS"):
        current_app.logger.debug(
            "PERF cache_invalidate namespace=%s mode=%s lock_wait_ms=%.1f per_company_invalidate_ms=%.1f "
            "per_namespace_invalidate_ms=%.1f version_bump_ms=%.1f redis_incr_ms=%.1f redis_get_ms=%.1f redis_set_ms=%.1f "
            "redis_delete_ms=%.1f scan_ms=%.1f delete_ms=%.1f local_cache_clear_ms=%.1f local_prune_ms=%.1f total_ms=%.1f",
            metrics["namespace"],
            metrics["mode"],
            metrics["lock_wait_ms"],
            metrics["per_company_invalidate_ms"],
            metrics["per_namespace_invalidate_ms"],
            metrics["version_bump_ms"],
            metrics["redis_incr_ms"],
            metrics["redis_get_ms"],
            metrics["redis_set_ms"],
            metrics["redis_delete_ms"],
            metrics["scan_ms"],
            metrics["delete_ms"],
            metrics["local_cache_clear_ms"],
            metrics["local_prune_ms"],
            metrics["total_ms"],
        )
    return metrics


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


def _bump_version(scope_type, company_id=None, namespace=None, _perf: dict | None = None, prefer_local_fast: bool = False):
    if prefer_local_fast and not _redis_required():
        started_local_at = time.perf_counter()
        with _LOCAL_LOCK:
            _prune_local_versions_locked()
            _LOCAL_VERSIONS[_version_key(scope_type, company_id, namespace)] = (
                _LOCAL_VERSIONS.get(_version_key(scope_type, company_id, namespace), 0) + 1
            )
        if _perf is not None:
            _perf["mode"] = "local"
            _perf["version_bump_ms"] = _perf.get("version_bump_ms", 0.0) + ((time.perf_counter() - started_local_at) * 1000)
            _perf["local_prune_ms"] = _perf.get("local_prune_ms", 0.0) + 0.0
        return

    client_started_at = time.perf_counter()
    client = _get_redis_client()
    if _perf is not None:
        _perf["redis_get_ms"] = _perf.get("redis_get_ms", 0.0) + ((time.perf_counter() - client_started_at) * 1000)
    version_key = _version_key(scope_type, company_id, namespace)
    if client is not None:
        try:
            redis_incr_started_at = time.perf_counter()
            client.incr(version_key)
            if _perf is not None:
                _perf["mode"] = "redis"
                _perf["redis_incr_ms"] = _perf.get("redis_incr_ms", 0.0) + ((time.perf_counter() - redis_incr_started_at) * 1000)
            return
        except _redis_transport_errors():
            _mark_redis_unavailable()
            if _redis_required():
                raise

    lock_wait_started_at = time.perf_counter()
    with _LOCAL_LOCK:
        lock_wait_ms = (time.perf_counter() - lock_wait_started_at) * 1000
        prune_started_at = time.perf_counter()
        _prune_local_versions_locked()
        prune_ms = (time.perf_counter() - prune_started_at) * 1000
        _LOCAL_VERSIONS[version_key] = _LOCAL_VERSIONS.get(version_key, 0) + 1
    if _perf is not None:
        _perf["mode"] = "local"
        _perf["lock_wait_ms"] = _perf.get("lock_wait_ms", 0.0) + lock_wait_ms
        _perf["local_prune_ms"] = _perf.get("local_prune_ms", 0.0) + prune_ms
        _perf["version_bump_ms"] = _perf.get("version_bump_ms", 0.0) + lock_wait_ms + prune_ms


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
        connect_timeout = _redis_timeout_seconds("REDIS_CONNECT_TIMEOUT_SECONDS", default=0.5)
        socket_timeout = _redis_timeout_seconds("REDIS_SOCKET_TIMEOUT_SECONDS", default=0.5)
        try:
            _REDIS_CLIENT = redis.Redis.from_url(
                url,
                decode_responses=True,
                socket_connect_timeout=connect_timeout,
                socket_timeout=socket_timeout,
            )
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


def _prune_local_cache_locked():
    now = time.monotonic()
    expired_keys = [
        cache_key
        for cache_key, entry in _LOCAL_CACHE.items()
        if float(entry.get("expires_at") or 0) <= now
    ]
    for cache_key in expired_keys:
        _LOCAL_CACHE.pop(cache_key, None)
    if len(_LOCAL_CACHE) <= _LOCAL_CACHE_MAX_ENTRIES:
        return
    overflow = len(_LOCAL_CACHE) - _LOCAL_CACHE_MAX_ENTRIES
    oldest_keys = sorted(
        _LOCAL_CACHE,
        key=lambda key: float(_LOCAL_CACHE[key].get("expires_at") or 0),
    )[:overflow]
    for cache_key in oldest_keys:
        _LOCAL_CACHE.pop(cache_key, None)


def _prune_local_versions_locked():
    if len(_LOCAL_VERSIONS) <= _LOCAL_VERSION_MAX_ENTRIES:
        return
    overflow = len(_LOCAL_VERSIONS) - _LOCAL_VERSION_MAX_ENTRIES
    removable_keys = [key for key in _LOCAL_VERSIONS if ":namespace:" in key][:overflow]
    for version_key in removable_keys:
        _LOCAL_VERSIONS.pop(version_key, None)


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


def _redis_timeout_seconds(env_name: str, default: float) -> float:
    raw = os.getenv(env_name)
    if raw is None:
        return default
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return default
    return max(0.05, value)
