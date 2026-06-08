import os
from pathlib import Path
import re
from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")


def _env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes", "on"}


def _env_int(name: str, default: int = 0) -> int:
    raw_value = os.getenv(name)
    if raw_value is None:
        return default
    try:
        return max(0, int(raw_value))
    except (TypeError, ValueError):
        return default


def _env_csv(name: str) -> list[str]:
    raw_value = os.getenv(name, "")
    if not raw_value:
        return []
    return [item.strip() for item in re.split(r"[,\n]", raw_value) if item.strip()]


class BaseConfig:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-andon-secret-key")
    ADMIN_PASSWORD = os.getenv("ANDON_ADMIN_PASSWORD")
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL")
    PRESS_RADIUS_DATABASE_URL = os.getenv("PRESS_RADIUS_DATABASE_URL")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": _env_flag("SQLALCHEMY_POOL_PRE_PING", "true"),
        "pool_recycle": int(os.getenv("SQLALCHEMY_POOL_RECYCLE", "300")),
        "pool_size": int(os.getenv("SQLALCHEMY_POOL_SIZE", "20")),
        "max_overflow": int(os.getenv("SQLALCHEMY_MAX_OVERFLOW", "40")),
        "pool_timeout": int(os.getenv("SQLALCHEMY_POOL_TIMEOUT", "30")),
        "pool_use_lifo": _env_flag("SQLALCHEMY_POOL_USE_LIFO", "true"),
    }
    JSON_SORT_KEYS = False
    TIMEZONE = os.getenv("APP_TIMEZONE", "America/Chicago")
    ANDON_DEFAULT_PAGE_SIZE = int(os.getenv("ANDON_DEFAULT_PAGE_SIZE", "50"))
    ANDON_AUTO_SCHEMA_MAINTENANCE = _env_flag("ANDON_AUTO_SCHEMA_MAINTENANCE")
    PREFERENCE_PAYLOAD_MAX_BYTES = int(os.getenv("PREFERENCE_PAYLOAD_MAX_BYTES", "16384"))
    LOGIN_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("LOGIN_RATE_LIMIT_WINDOW_SECONDS", "300"))
    LOGIN_RATE_LIMIT_MAX_ATTEMPTS = int(os.getenv("LOGIN_RATE_LIMIT_MAX_ATTEMPTS", "8"))
    # Pilot mode: keep Redis disabled and use in-process cache only.
    REDIS_URL = None
    REDIS_REQUIRED = False
    SOCKETIO_ENABLED = _env_flag("SOCKETIO_ENABLED", "true")
    SOCKETIO_MESSAGE_QUEUE = None
    SOCKETIO_ASYNC_MODE = os.getenv("SOCKETIO_ASYNC_MODE") or "threading"
    SOCKETIO_ALLOW_MULTIWORKER = _env_flag("SOCKETIO_ALLOW_MULTIWORKER")
    SOCKETIO_FORCE_POLLING = _env_flag("SOCKETIO_FORCE_POLLING")
    SOCKETIO_PING_INTERVAL = int(os.getenv("SOCKETIO_PING_INTERVAL", "25"))
    SOCKETIO_PING_TIMEOUT = int(os.getenv("SOCKETIO_PING_TIMEOUT", "20"))
    SOCKETIO_HTTP_COMPRESSION = _env_flag("SOCKETIO_HTTP_COMPRESSION", "true")
    PAGER_ACTIVE_ALERTS_QUERY_TIMEOUT_MS = int(os.getenv("PAGER_ACTIVE_ALERTS_QUERY_TIMEOUT_MS", "2000"))
    PAGER_AUTH_LEGACY_FALLBACK_LIMIT = _env_int("PAGER_AUTH_LEGACY_FALLBACK_LIMIT", 25)
    OPERATOR_METADATA_CACHE_TTL_SECONDS = _env_int("OPERATOR_METADATA_CACHE_TTL_SECONDS", 60)
    OPERATOR_METADATA_INCLUDE_PROBLEM_DESCRIPTION = _env_flag("OPERATOR_METADATA_INCLUDE_PROBLEM_DESCRIPTION")
    OPERATOR_METADATA_MAX_PROBLEMS = _env_int("OPERATOR_METADATA_MAX_PROBLEMS", 20000)
    OPERATOR_SNAPSHOT_CACHE_TTL_SECONDS = _env_int("OPERATOR_SNAPSHOT_CACHE_TTL_SECONDS", 8)
    PRESS_RADIUS_CACHE_TTL_SECONDS = _env_int("PRESS_RADIUS_CACHE_TTL_SECONDS", 10)
    PRESS_RADIUS_CONNECT_TIMEOUT_SECONDS = _env_int("PRESS_RADIUS_CONNECT_TIMEOUT_SECONDS", 2)
    PRESS_RADIUS_STATEMENT_TIMEOUT_MS = _env_int("PRESS_RADIUS_STATEMENT_TIMEOUT_MS", 2500)
    SESSION_COOKIE_HTTPONLY = True
    USER_PASSWORD_HASH_METHOD = os.getenv("USER_PASSWORD_HASH_METHOD", "pbkdf2:sha256:120000")
    USER_PASSWORD_SALT_LENGTH = int(os.getenv("USER_PASSWORD_SALT_LENGTH", "16"))
    SESSION_COOKIE_SAMESITE = os.getenv("SESSION_COOKIE_SAMESITE", "Lax")
    SESSION_COOKIE_SECURE = _env_flag("SESSION_COOKIE_SECURE")
    SESSION_COOKIE_DOMAIN = os.getenv("SESSION_COOKIE_DOMAIN") or None
    ANDON_PERF_LOGS = _env_flag("ANDON_PERF_LOGS")
    ANDON_HTTP_ACCESS_LOGS = _env_flag("ANDON_HTTP_ACCESS_LOGS")
    ANDON_SOCKET_DEBUG_LOGS = _env_flag("ANDON_SOCKET_DEBUG_LOGS")
    ANDON_DEEP_DEBUG_ALERT_LIFECYCLE = _env_flag("ANDON_DEEP_DEBUG_ALERT_LIFECYCLE")
    ANDON_PERF_FOCUS = (os.getenv("ANDON_PERF_FOCUS") or "").strip().lower()
    ANDON_PERF_FOCUS_PATTERNS = _env_csv("ANDON_PERF_FOCUS_PATTERNS")
    ANDON_PAGER_API_ONLY = _env_flag("ANDON_PAGER_API_ONLY")
    PREFERRED_URL_SCHEME = os.getenv("PREFERRED_URL_SCHEME") or ("https" if SESSION_COOKIE_SECURE else "http")
    PROXY_FIX_X_FOR = _env_int("PROXY_FIX_X_FOR", 0)
    PROXY_FIX_X_PROTO = _env_int("PROXY_FIX_X_PROTO", 0)
    PROXY_FIX_X_HOST = _env_int("PROXY_FIX_X_HOST", 0)
    PROXY_FIX_X_PORT = _env_int("PROXY_FIX_X_PORT", 0)
    PROXY_FIX_X_PREFIX = _env_int("PROXY_FIX_X_PREFIX", 0)

    ESCALATION_EMAIL_ENABLED = _env_flag("ESCALATION_EMAIL_ENABLED")
    ESCALATION_INLINE_CHECKS_ENABLED = _env_flag("ESCALATION_INLINE_CHECKS_ENABLED", "true")
    ESCALATION_EMAIL_SMTP_HOST = os.getenv("ESCALATION_EMAIL_SMTP_HOST")
    ESCALATION_EMAIL_SMTP_PORT = int(os.getenv("ESCALATION_EMAIL_SMTP_PORT", "587"))
    ESCALATION_EMAIL_SMTP_USER = os.getenv("ESCALATION_EMAIL_SMTP_USER")
    ESCALATION_EMAIL_SMTP_PASSWORD = os.getenv("ESCALATION_EMAIL_SMTP_PASSWORD")
    ESCALATION_EMAIL_FROM = os.getenv("ESCALATION_EMAIL_FROM")
    ESCALATION_EMAIL_USE_TLS = _env_flag("ESCALATION_EMAIL_USE_TLS", "true")
    ESCALATION_EMAIL_USE_SSL = _env_flag("ESCALATION_EMAIL_USE_SSL")

    ESCALATION_MANUAL_USER_1_NAME = os.getenv("ESCALATION_MANUAL_USER_1_NAME")
    ESCALATION_MANUAL_USER_1_EMAIL = os.getenv("ESCALATION_MANUAL_USER_1_EMAIL")
    ESCALATION_MANUAL_USER_2_NAME = os.getenv("ESCALATION_MANUAL_USER_2_NAME")
    ESCALATION_MANUAL_USER_2_EMAIL = os.getenv("ESCALATION_MANUAL_USER_2_EMAIL")
    ESCALATION_MANUAL_USER_3_NAME = os.getenv("ESCALATION_MANUAL_USER_3_NAME")
    ESCALATION_MANUAL_USER_3_EMAIL = os.getenv("ESCALATION_MANUAL_USER_3_EMAIL")
    ESCALATION_MANUAL_USER_4_NAME = os.getenv("ESCALATION_MANUAL_USER_4_NAME")
    ESCALATION_MANUAL_USER_4_EMAIL = os.getenv("ESCALATION_MANUAL_USER_4_EMAIL")
    ESCALATION_MANUAL_USER_5_NAME = os.getenv("ESCALATION_MANUAL_USER_5_NAME")
    ESCALATION_MANUAL_USER_5_EMAIL = os.getenv("ESCALATION_MANUAL_USER_5_EMAIL")


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL")
    ANDON_AUTO_SCHEMA_MAINTENANCE = False
    ANDON_PERF_LOGS = _env_flag("ANDON_PERF_LOGS")
    ANDON_HTTP_ACCESS_LOGS = _env_flag("ANDON_HTTP_ACCESS_LOGS")
    SQLALCHEMY_ENGINE_OPTIONS = {
        **BaseConfig.SQLALCHEMY_ENGINE_OPTIONS,
        "pool_pre_ping": False,
    }


class TestingConfig(BaseConfig):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = os.getenv("TEST_DATABASE_URL") or "sqlite:///:memory:"


class ProductionConfig(BaseConfig):
    DEBUG = False
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL")
    SESSION_COOKIE_SECURE = _env_flag("SESSION_COOKIE_SECURE", "true")
    ESCALATION_INLINE_CHECKS_ENABLED = _env_flag("ESCALATION_INLINE_CHECKS_ENABLED")
    PREFERRED_URL_SCHEME = os.getenv("PREFERRED_URL_SCHEME") or "https"
    PROXY_FIX_X_FOR = _env_int("PROXY_FIX_X_FOR", 1)
    PROXY_FIX_X_PROTO = _env_int("PROXY_FIX_X_PROTO", 1)


config_by_name = {
    "default": DevelopmentConfig,
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}
