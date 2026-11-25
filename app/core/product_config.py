"""Opt-in product dependencies, independent of production/default application data."""
from functools import lru_cache
from pathlib import Path
from pydantic_settings import BaseSettings, SettingsConfigDict


class ProductSettings(BaseSettings):
    model_config = SettingsConfigDict(env_prefix="PRODUCT_", env_file=None, extra="ignore")

    es_url: str = ""
    es_index: str = "sirius-product-menu-v1"
    state_dir: Path = Path.home() / ".local/state/siriusfood/product"
    vector_dsn: str = ""
    embedding_path: str = ""
    embedding_revision: str = ""
    bai_secret_file: Path = Path.home() / ".config/siriusfood/bai.env"
    pricing_file: str = ""
    generation_purpose: str = "test"
    frontend_dist: str = ""


@lru_cache
def product_settings() -> ProductSettings:
    return ProductSettings()
