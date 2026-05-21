import os
from pathlib import Path

from dotenv import load_dotenv


BASE_DIR = Path(__file__).resolve().parents[1]
load_dotenv(BASE_DIR / ".env")


def _env_flag(name: str, default: str = "false") -> bool:
    return os.getenv(name, default).lower() in {"1", "true", "yes", "on"}


def _local_sqlite_uri() -> str:
    sqlite_path = os.getenv("LOCAL_SQLITE_PATH") or str(BASE_DIR / "instance" / "andon_local.sqlite3")
    return f"sqlite:///{Path(sqlite_path).expanduser()}"


def _development_database_uri() -> str | None:
    database_url = os.getenv("DATABASE_URL")
    if database_url:
        return database_url
    if _env_flag("LOCAL_SQLITE_FALLBACK", "true"):
        return _local_sqlite_uri()
    return None


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
    ANDON_RUNTIME_SCHEMA_REPAIR = _env_flag("ANDON_RUNTIME_SCHEMA_REPAIR", "true")
    PREFERENCE_PAYLOAD_MAX_BYTES = int(os.getenv("PREFERENCE_PAYLOAD_MAX_BYTES", "16384"))
    LOGIN_RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("LOGIN_RATE_LIMIT_WINDOW_SECONDS", "300"))
    LOGIN_RATE_LIMIT_MAX_ATTEMPTS = int(os.getenv("LOGIN_RATE_LIMIT_MAX_ATTEMPTS", "8"))
    REDIS_URL = os.getenv("REDIS_URL")
    REDIS_REQUIRED = _env_flag("REDIS_REQUIRED")
    SOCKETIO_ENABLED = _env_flag("SOCKETIO_ENABLED", "true")
    SOCKETIO_MESSAGE_QUEUE = os.getenv("SOCKETIO_MESSAGE_QUEUE") or REDIS_URL
    SOCKETIO_ASYNC_MODE = os.getenv("SOCKETIO_ASYNC_MODE") or "threading"
    SOCKETIO_ALLOW_MULTIWORKER = _env_flag("SOCKETIO_ALLOW_MULTIWORKER")
    SOCKETIO_FORCE_POLLING = _env_flag("SOCKETIO_FORCE_POLLING")
    SOCKETIO_PING_INTERVAL = int(os.getenv("SOCKETIO_PING_INTERVAL", "25"))
    SOCKETIO_PING_TIMEOUT = int(os.getenv("SOCKETIO_PING_TIMEOUT", "20"))
    SOCKETIO_HTTP_COMPRESSION = _env_flag("SOCKETIO_HTTP_COMPRESSION", "true")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = os.getenv("SESSION_COOKIE_SAMESITE", "Lax")
    SESSION_COOKIE_SECURE = _env_flag("SESSION_COOKIE_SECURE")
    ANDON_PERF_LOGS = _env_flag("ANDON_PERF_LOGS")
    ANDON_PAGER_API_ONLY = _env_flag("ANDON_PAGER_API_ONLY")

    ESCALATION_EMAIL_ENABLED = _env_flag("ESCALATION_EMAIL_ENABLED")
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
    SQLALCHEMY_DATABASE_URI = _development_database_uri()
    ANDON_AUTO_SCHEMA_MAINTENANCE = True
    ANDON_PERF_LOGS = True
    SQLALCHEMY_ENGINE_OPTIONS = {
        **BaseConfig.SQLALCHEMY_ENGINE_OPTIONS,
        "pool_pre_ping": False,
    }


class TestingConfig(BaseConfig):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = os.getenv("TEST_DATABASE_URL") or _development_database_uri()


class ProductionConfig(BaseConfig):
    DEBUG = False
    SQLALCHEMY_DATABASE_URI = os.getenv("DATABASE_URL")
    ANDON_RUNTIME_SCHEMA_REPAIR = _env_flag("ANDON_RUNTIME_SCHEMA_REPAIR")
    SESSION_COOKIE_SECURE = _env_flag("SESSION_COOKIE_SECURE", "true")


config_by_name = {
    "default": DevelopmentConfig,
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}
