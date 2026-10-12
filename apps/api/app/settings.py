import ipaddress
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlsplit

from email_validator import EmailNotValidError, validate_email
from pydantic import SecretStr, field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore", hide_input_in_errors=True)

    log_level: str = "INFO"
    app_environment: str = "development"
    app_component: str = "api"
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
    saml_public_base_url: str | None = None
    # Trusted deployment origin for externally returned SCIM resource URLs.
    scim_public_base_url: str | None = None
    saml_sp_key_file: Path | None = None
    saml_sp_certificate_file: Path | None = None
    saml_xmlsec_binary: str = "xmlsec1"
    saml_rate_limit: int = 20
    saml_rate_window_seconds: int = 300

    @field_validator("app_environment")
    @classmethod
    def validate_app_environment(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in {"development", "test", "staging", "production"}:
            raise ValueError("APP_ENVIRONMENT must be development, test, staging, or production.")
        return normalized

    @field_validator("app_component")
    @classmethod
    def validate_app_component(cls, value: str) -> str:
        normalized = value.strip().casefold()
        if normalized not in {"api", "worker"}:
            raise ValueError("APP_COMPONENT must be api or worker.")
        return normalized

    @field_validator("scim_public_base_url")
    @classmethod
    def validate_scim_public_base_url(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        return _https_origin(value, "SCIM_PUBLIC_BASE_URL")

    @field_validator("saml_public_base_url")
    @classmethod
    def validate_saml_public_base_url(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        return _https_origin(value, "SAML_PUBLIC_BASE_URL")

    @field_validator("smtp_from_address")
    @classmethod
    def validate_smtp_from_address(cls, value: str | None) -> str | None:
        if value is None or value == "":
            return None
        try:
            return validate_email(value, check_deliverability=False).normalized
        except EmailNotValidError:
            raise ValueError("SMTP_FROM_ADDRESS must be a valid email address.") from None

    @property
    def is_production_like(self) -> bool:
        return self.app_environment in {"production", "staging"}

    @property
    def psycopg_database_url(self) -> str:
        # SQLAlchemy's driver-qualified URL is not accepted by psycopg.connect.
        return self.database_url.replace("postgresql+psycopg://", "postgresql://", 1)

    @model_validator(mode="after")
    def reject_insecure_production_defaults(self) -> "Settings":
        if self.is_production_like:
            secret = self.auth_secret_key.get_secret_value()
            if secret.startswith("change-me-") or len(secret) < 32:
                raise ValueError(
                    "AUTH_SECRET_KEY must be explicitly configured in production-like environments."
                )
            _validate_service_url(
                self.database_url, "DATABASE_URL", {"postgres", "postgresql", "postgresql+psycopg"}
            )
            _validate_service_url(self.redis_url, "REDIS_URL", {"redis", "rediss"})
            if self.app_component == "api":
                if "*" in self.cors_origins:
                    raise ValueError(
                        "Wildcard CORS origins are not allowed in production-like environments."
                    )
                try:
                    for origin in self.cors_origins:
                        if origin != _https_url(origin, "CORS_ORIGINS", allow_path=False):
                            raise ValueError
                except ValueError:
                    raise ValueError(
                        "CORS_ORIGINS must contain only explicit HTTPS origins in "
                        "production-like environments."
                    ) from None
                if not self.cors_origins:
                    raise ValueError("CORS_ORIGINS must contain an explicit HTTPS origin.")
                if not self.allowed_hosts or any(
                    host == "*"
                    or _is_local_host(host)
                    or not host.strip()
                    or any(character in host for character in "/\\@?# ")
                    for host in self.allowed_hosts
                ):
                    raise ValueError(
                        "ALLOWED_HOSTS must contain explicit non-local production hostnames."
                    )
                if not self.rate_limit_enabled:
                    raise ValueError(
                        "RATE_LIMIT_ENABLED must remain enabled in production-like environments."
                    )
                _https_url(self.frontend_base_url, "FRONTEND_BASE_URL", allow_path=True)
                if not 1 <= self.smtp_port <= 65535:
                    raise ValueError("SMTP_PORT must be between 1 and 65535.")
                if not self.smtp_host or not self.smtp_from_address:
                    raise ValueError(
                        "SMTP_HOST and SMTP_FROM_ADDRESS are required in "
                        "production-like environments."
                    )
                if self.smtp_starttls == self.smtp_use_ssl:
                    raise ValueError(
                        "Exactly one of SMTP_STARTTLS or SMTP_USE_SSL must be enabled "
                        "in production-like environments."
                    )
                if bool(self.smtp_username) != bool(self.smtp_password):
                    raise ValueError("SMTP_USERNAME and SMTP_PASSWORD must be configured together.")
                if not self.saml_public_base_url:
                    raise ValueError(
                        "SAML_PUBLIC_BASE_URL must be configured for the production-like API."
                    )
                if not self.scim_public_base_url:
                    raise ValueError(
                        "SCIM_PUBLIC_BASE_URL must be configured for the production-like API."
                    )
                if self.google_oauth_client_id:
                    _https_url(
                        self.google_oauth_redirect_uri, "GOOGLE_OAUTH_REDIRECT_URI", allow_path=True
                    )
        if self.smtp_starttls and self.smtp_use_ssl:
            raise ValueError("SMTP_STARTTLS and SMTP_USE_SSL cannot both be enabled.")
        if bool(self.google_oauth_client_id) != bool(self.google_oauth_client_secret):
            raise ValueError(
                "GOOGLE_OAUTH_CLIENT_ID and GOOGLE_OAUTH_CLIENT_SECRET must be configured together."
            )
        if bool(self.saml_sp_key_file) != bool(self.saml_sp_certificate_file):
            raise ValueError("SAML SP key and certificate must be configured together.")
        return self


def _is_local_host(host: str) -> bool:
    normalized = host.strip().strip("[]").casefold()
    if normalized in {"localhost", "testserver"} or normalized.endswith(".localhost"):
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _https_url(value: str, name: str, *, allow_path: bool) -> str:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        raise ValueError(f"{name} must be an explicit HTTPS URL.") from None
    if (
        parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username is not None
        or parsed.password is not None
        or (not allow_path and parsed.path not in {"", "/"})
        or parsed.query
        or parsed.fragment
        or "\\" in value
        or any(character.isspace() for character in value)
        or _is_local_host(parsed.hostname)
    ):
        raise ValueError(f"{name} must be an explicit HTTPS URL.")
    return value.rstrip("/")


def _https_origin(value: str, name: str) -> str:
    return _https_url(value, name, allow_path=False)


def _validate_service_url(value: str, name: str, schemes: set[str]) -> None:
    try:
        parsed = urlsplit(value)
        _ = parsed.port
    except ValueError:
        raise ValueError(f"{name} must use a supported non-local service URL.") from None
    if (
        parsed.scheme not in schemes
        or not parsed.hostname
        or _is_local_host(parsed.hostname)
        or not parsed.path
    ):
        raise ValueError(f"{name} must use a supported non-local service URL.")


@lru_cache
def get_settings() -> Settings:
    return Settings()


settings = get_settings()
