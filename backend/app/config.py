from functools import lru_cache

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    app_name: str = "Kairo Persistence Motor"
    environment: str = "development"
    database_url: str = Field(validation_alias="KAIRO_DATABASE_URL")
    runtime_database_url: str = Field(validation_alias="KAIRO_RUNTIME_DATABASE_URL")
    runtime_database_user: str = Field(
        default="kairo_runtime", validation_alias="KAIRO_RUNTIME_USER"
    )
    runtime_database_password: str = Field(
        default="", validation_alias="KAIRO_RUNTIME_PASSWORD"
    )


@lru_cache
def get_settings() -> Settings:
    return Settings()
