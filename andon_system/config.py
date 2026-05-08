import os
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
INSTANCE_DIR = BASE_DIR / "instance"
INSTANCE_DIR.mkdir(exist_ok=True)


class BaseConfig:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-andon-secret-key")
    SQLALCHEMY_DATABASE_URI = os.getenv(
        "DATABASE_URL",
        f"sqlite:///{(INSTANCE_DIR / 'andon.db').as_posix()}",
    )
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {
        "pool_pre_ping": True,
        "pool_recycle": int(os.getenv("SQLALCHEMY_POOL_RECYCLE", "300")),
    }
    JSON_SORT_KEYS = False
    TIMEZONE = os.getenv("APP_TIMEZONE", "America/Chicago")
    ANDON_DEFAULT_PAGE_SIZE = int(os.getenv("ANDON_DEFAULT_PAGE_SIZE", "50"))
    ANDON_AUTO_SCHEMA_MAINTENANCE = os.getenv("ANDON_AUTO_SCHEMA_MAINTENANCE", "false").lower() in {"1", "true", "yes", "on"}
    REDIS_URL = os.getenv("REDIS_URL")
    REDIS_REQUIRED = os.getenv("REDIS_REQUIRED", "false").lower() in {"1", "true", "yes", "on"}
    SOCKETIO_ENABLED = os.getenv("SOCKETIO_ENABLED", "true").lower() in {"1", "true", "yes", "on"}
    SOCKETIO_MESSAGE_QUEUE = os.getenv("SOCKETIO_MESSAGE_QUEUE") or REDIS_URL
    SOCKETIO_ASYNC_MODE = os.getenv("SOCKETIO_ASYNC_MODE") or None


class DevelopmentConfig(BaseConfig):
    DEBUG = True
    ANDON_AUTO_SCHEMA_MAINTENANCE = True


class TestingConfig(BaseConfig):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = os.getenv("TEST_DATABASE_URL", "sqlite:///:memory:")
    ANDON_AUTO_SCHEMA_MAINTENANCE = True


class ProductionConfig(BaseConfig):
    DEBUG = False


config_by_name = {
    "default": DevelopmentConfig,
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}
