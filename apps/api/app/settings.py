from functools import lru_cache
from pathlib import Path

from pydantic import SecretStr, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    log_level: str = "INFO"
    app_environment: str = "development"
    cors_origins: list[str] = [
        "http://localhost:5173",
        "http://localhost:5174",
        "http://127.0.0.1:5173",
        "http://127.0.0.1:5174",
    ]
    allowed_hosts: list[str] = ["localhost", "127.0.0.1", "testserver"]
    database_url: str = "postgresql://document_analyzer:change-me-for-local-development@localhost:5432/document_analyzer"
    redis_url: str = "redis://localhost:6379/0"
    document_queue_name: str = "document-processing"
    queue_reconciliation_interval_seconds: int = 30
    storage_path: Path = Path("storage/documents")
    max_upload_size: int = 10 * 1024 * 1024
    # Leave unset to discover `tesseract` on PATH (the Docker image installs it).
    # Native installations can override this with TESSERACT_EXECUTABLE.
    tesseract_executable: Path | None = None
    ocr_render_scale: float = 2.0
    max_pdf_pages: int = 100
    max_image_pixels: int = 50_000_000
    ocr_timeout_seconds: int = 120
    auth_secret_key: SecretStr = SecretStr("change-me-before-production-use-a-random-secret")
    auth_access_token_expire_minutes: int = 60
    rate_limit_enabled: bool = False
    login_rate_limit: int = 10
    login_rate_window_seconds: int = 300
    registration_rate_limit: int = 5
    registration_rate_window_seconds: int = 3600
    upload_rate_limit: int = 30
    upload_rate_window_seconds: int = 60
    password_reset_token_expire_minutes: int = 20
    forgot_password_rate_limit: int = 5
    forgot_password_rate_window_seconds: int = 3600
    reset_password_rate_limit: int = 10
    reset_password_rate_window_seconds: int = 900
    frontend_base_url: str = "http://localhost:5173"
    smtp_host: str | None = None
    smtp_port: int = 587
    smtp_username: str | None = None
    smtp_password: SecretStr | None = None
    smtp_from_address: str | None = None
    smtp_starttls: bool = True
    smtp_use_ssl: bool = False
    password_reset_dev_outbox_path: Path = Path("storage/password-reset-outbox")
    google_oauth_client_id: str | None = None
    google_oauth_client_secret: SecretStr | None = None
    google_oauth_redirect_uri: str = "http://localhost:8000/auth/google/callback"
    google_oauth_transaction_expire_seconds: int = 600
    google_oauth_login_code_expire_seconds: int = 60
    google_oauth_rate_limit: int = 20
    google_oauth_rate_window_seconds: int = 300

    @model_validator(mode="after")
    def reject_insecure_production_defaults(self) -> "Settings":
        if self.app_environment.casefold() == "production":
            secret = self.auth_secret_key.get_secret_value()
            if secret.startswith("change-me-") or len(secret) < 32:
                raise ValueError("AUTH_SECRET_KEY must be explicitly configured in production.")
            if "change-me-for-local-development" in self.database_url:
                raise ValueError("DATABASE_URL must not use development credentials in production.")
            if "*" in self.cors_origins:
                raise ValueError("Wildcard CORS origins are not allowed in production.")
            if not self.cors_origins or any(
                not origin.startswith("https://") for origin in self.cors_origins
            ):
                raise ValueError(
                    "CORS_ORIGINS must contain only explicit HTTPS origins in production."
                )
            if not self.allowed_hosts or "*" in self.allowed_hosts:
                raise ValueError("ALLOWED_HOSTS must be explicitly restricted in production.")
            if not self.rate_limit_enabled:
                raise ValueError("RATE_LIMIT_ENABLED must remain enabled in production.")
            if not self.frontend_base_url.startswith("https://"):
                raise ValueError("FRONTEND_BASE_URL must use HTTPS in production.")
            if not self.smtp_host or not self.smtp_from_address:
                raise ValueError("SMTP_HOST and SMTP_FROM_ADDRESS are required in production.")
            if self.google_oauth_client_id and not self.google_oauth_redirect_uri.startswith(
                "https://"
            ):
                raise ValueError("GOOGLE_OAUTH_REDIRECT_URI must use HTTPS in production.")
        if self.smtp_starttls and self.smtp_use_ssl:
            raise ValueError("SMTP_STARTTLS and SMTP_USE_SSL cannot both be enabled.")
        if bool(self.google_oauth_client_id) != bool(self.google_oauth_client_secret):
            raise ValueError(
                "GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET must be configured together."
            )
        return self


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
