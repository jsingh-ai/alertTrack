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
    }
    JSON_SORT_KEYS = False
    TIMEZONE = os.getenv("APP_TIMEZONE", "America/Chicago")
    ANDON_DEFAULT_PAGE_SIZE = int(os.getenv("ANDON_DEFAULT_PAGE_SIZE", "50"))


class DevelopmentConfig(BaseConfig):
    DEBUG = True


class TestingConfig(BaseConfig):
    TESTING = True
    SQLALCHEMY_DATABASE_URI = os.getenv("TEST_DATABASE_URL", "sqlite:///:memory:")


class ProductionConfig(BaseConfig):
    DEBUG = False


config_by_name = {
    "default": DevelopmentConfig,
    "development": DevelopmentConfig,
    "testing": TestingConfig,
    "production": ProductionConfig,
}
