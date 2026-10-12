# ruff: noqa: E501

import uuid
from datetime import date, datetime
from decimal import Decimal
from enum import StrEnum

from sqlalchemy import (
    JSON,
    BigInteger,
    Boolean,
    CheckConstraint,
    Date,
    DateTime,
    Enum,
    ForeignKey,
    ForeignKeyConstraint,
    Index,
    Integer,
    Numeric,
    String,
    Text,
    UniqueConstraint,
    column,
    func,
)
from sqlalchemy.dialects.postgresql import UUID
from sqlalchemy.orm import Mapped, mapped_column, relationship

from apps.api.app.database import Base


class DocumentStatus(StrEnum):
    UPLOADED = "uploaded"
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ProcessingOperationStatus(StrEnum):
    QUEUED = "queued"
    PROCESSING = "processing"
    COMPLETED = "completed"
    FAILED = "failed"


class ProcessingPriority(StrEnum):
    NORMAL = "normal"
    PRIORITY = "priority"


class UserRole(StrEnum):
    ADMIN = "admin"
    REVIEWER = "reviewer"
    VIEWER = "viewer"


class OrganizationStatus(StrEnum):
    ACTIVE = "active"
    INACTIVE = "inactive"


class OrganizationMemberRole(StrEnum):
    OWNER = "owner"
    ADMIN = "admin"
    MEMBER = "member"
    # Retained for compatibility with historical memberships. New commercial
    # invitations and UI never create or offer these roles.
    REVIEWER = "reviewer"
    VIEWER = "viewer"


class OrganizationMemberStatus(StrEnum):
    PENDING = "pending"
    ACTIVE = "active"
    INACTIVE = "inactive"


class OrganizationInvitationStatus(StrEnum):
    PENDING = "pending"
    ACCEPTED = "accepted"
    REVOKED = "revoked"
    EXPIRED = "expired"


class WebhookDeliveryStatus(StrEnum):
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    RETRYING = "retrying"
    FAILED = "failed"


class AIAnalysisStatus(StrEnum):
    PENDING = "pending"
    COMPLETED = "completed"
    FAILED = "failed"


class AIFindingSeverity(StrEnum):
    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class AIFindingStatus(StrEnum):
    OPEN = "open"
    ACKNOWLEDGED = "acknowledged"
    RESOLVED = "resolved"


class AIFindingCategory(StrEnum):
    ARITHMETIC = "arithmetic_anomaly"
    DUPLICATE = "duplicate_invoice"
    AMOUNT = "unusual_amount"
    VENDOR = "vendor_pattern"
    MISSING = "missing_information"
    CONFIDENCE = "low_confidence_extraction"
    DATE = "date_anomaly"
    TAX = "tax_consistency"


class InvalidDocumentStatusTransition(ValueError):
    pass


class User(Base):
    __tablename__ = "users"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    email: Mapped[str] = mapped_column(String(320), nullable=False, unique=True)
    display_name: Mapped[str | None] = mapped_column(String(120))
    self_deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), index=True)
    email_verified: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    password_hash: Mapped[str] = mapped_column(String(255), nullable=False)
    auth_version: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    force_password_change: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    role: Mapped[UserRole] = mapped_column(
        Enum(
            UserRole,
            name="user_role",
            values_callable=lambda roles: [role.value for role in roles],
        ),
        nullable=False,
        default=UserRole.REVIEWER,
        server_default=UserRole.REVIEWER.value,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    documents: Mapped[list["Document"]] = relationship(
        back_populates="owner", foreign_keys="Document.owner_id"
    )
    created_documents: Mapped[list["Document"]] = relationship(
        back_populates="created_by_user", foreign_keys="Document.created_by_user_id"
    )
    organization_memberships: Mapped[list["OrganizationMember"]] = relationship(
        back_populates="user"
    )
    oauth_identities: Mapped[list["OAuthIdentity"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    oauth_login_codes: Mapped[list["OAuthLoginCode"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    password_reset_tokens: Mapped[list["PasswordResetToken"]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )


class ScimConfiguration(Base):
    __tablename__ = "organization_scim_configurations"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ScimCredential(Base):
    __tablename__ = "scim_credentials"
    __table_args__ = (UniqueConstraint("organization_id", "label"),)
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class ScimUserMapping(Base):
    __tablename__ = "scim_user_mappings"
    __table_args__ = (
        UniqueConstraint("organization_id", "user_id"),
        UniqueConstraint("organization_id", "external_id"),
        Index(
            "uq_scim_org_username_ci",
            "organization_id",
            func.lower(column("user_name")),
            unique=True,
        ),
        ForeignKeyConstraint(
            ["organization_id", "user_id"],
            ["organization_members.organization_id", "organization_members.user_id"],
            ondelete="CASCADE",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    external_id: Mapped[str | None] = mapped_column(String(255), nullable=True)
    user_name: Mapped[str] = mapped_column(String(320), nullable=False)
    work_email: Mapped[str | None] = mapped_column(String(320), nullable=True)
    version: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class ScimProvisioningEvent(Base):
    __tablename__ = "scim_provisioning_events"
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    credential_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("scim_credentials.id", ondelete="SET NULL"),
        nullable=True,
        index=True,
    )
    resource_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True), nullable=True)
    action: Mapped[str] = mapped_column(String(64), nullable=False)
    outcome: Mapped[str] = mapped_column(String(32), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Organization(Base):
    __tablename__ = "organizations"
    __table_args__ = (
        CheckConstraint(
            "NOT sso_enforced OR sso_recovery_code_hash IS NOT NULL",
            name="ck_organization_sso_enforcement_recovery",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    name: Mapped[str] = mapped_column(String(255), nullable=False)
    slug: Mapped[str] = mapped_column(String(100), nullable=False, unique=True, index=True)
    plan_code: Mapped[str] = mapped_column(
        String(50), nullable=False, default="standard", server_default="standard", index=True
    )
    status: Mapped[OrganizationStatus] = mapped_column(
        Enum(
            OrganizationStatus,
            name="organization_status",
            values_callable=lambda statuses: [status.value for status in statuses],
        ),
        nullable=False,
        default=OrganizationStatus.ACTIVE,
        server_default=OrganizationStatus.ACTIVE.value,
        index=True,
    )
    sso_enforced: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    sso_recovery_code_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    memberships: Mapped[list["OrganizationMember"]] = relationship(back_populates="organization")
    documents: Mapped[list["Document"]] = relationship(back_populates="organization")
    invitations: Mapped[list["OrganizationInvitation"]] = relationship(
        back_populates="organization"
    )
    processing_operations: Mapped[list["ProcessingOperation"]] = relationship(
        back_populates="organization"
    )
    enterprise_quota: Mapped["OrganizationEnterpriseQuota | None"] = relationship(
        back_populates="organization", uselist=False, cascade="all, delete-orphan"
    )
    api_credentials: Mapped[list["ApiCredential"]] = relationship(back_populates="organization")
    webhook_endpoints: Mapped[list["WebhookEndpoint"]] = relationship(back_populates="organization")
    saml_configuration: Mapped["OrganizationSamlConfiguration | None"] = relationship(
        back_populates="organization", uselist=False, cascade="all, delete-orphan"
    )


class OrganizationMember(Base):
    __tablename__ = "organization_members"
    __table_args__ = (UniqueConstraint("organization_id", "user_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    role: Mapped[OrganizationMemberRole] = mapped_column(
        Enum(
            OrganizationMemberRole,
            name="organization_member_role",
            values_callable=lambda roles: [role.value for role in roles],
        ),
        nullable=False,
        default=OrganizationMemberRole.VIEWER,
        server_default=OrganizationMemberRole.VIEWER.value,
    )
    status: Mapped[OrganizationMemberStatus] = mapped_column(
        Enum(
            OrganizationMemberStatus,
            name="organization_member_status",
            values_callable=lambda statuses: [status.value for status in statuses],
        ),
        nullable=False,
        default=OrganizationMemberStatus.PENDING,
        server_default=OrganizationMemberStatus.PENDING.value,
        index=True,
    )
    joined_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    deactivated_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    organization: Mapped[Organization] = relationship(back_populates="memberships")
    user: Mapped[User] = relationship(back_populates="organization_memberships")


class UserSession(Base):
    __tablename__ = "user_sessions"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    auth_version: Mapped[int] = mapped_column(Integer, nullable=False)
    sso_organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True, index=True
    )


class OrganizationSecurityAuditEvent(Base):
    __tablename__ = "organization_security_audit_events"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    actor_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    subject_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True
    )
    action: Mapped[str] = mapped_column(String(80), nullable=False)
    outcome: Mapped[str] = mapped_column(
        String(24), nullable=False, default="success", server_default="success"
    )
    resource_type: Mapped[str | None] = mapped_column(String(80), nullable=True)
    resource_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    context: Mapped[dict] = mapped_column(JSON, nullable=False, default=dict, server_default="{}")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )


class OrganizationEnterpriseQuota(Base):
    __tablename__ = "organization_enterprise_quotas"
    __table_args__ = (
        CheckConstraint("monthly_processing_limit >= 5001", name="ck_enterprise_processing_min"),
        CheckConstraint("active_seat_limit >= 6", name="ck_enterprise_seats_min"),
        CheckConstraint("storage_bytes_limit >= 11000000000", name="ck_enterprise_storage_min"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    monthly_processing_limit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    active_seat_limit: Mapped[int] = mapped_column(Integer, nullable=False)
    storage_bytes_limit: Mapped[int] = mapped_column(BigInteger, nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    organization: Mapped[Organization] = relationship(back_populates="enterprise_quota")


class OrganizationSamlConfiguration(Base):
    __tablename__ = "organization_saml_configurations"
    __table_args__ = (UniqueConstraint("id", "organization_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        unique=True,
        nullable=False,
    )
    enabled: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false"
    )
    idp_entity_id: Mapped[str] = mapped_column(String(2048), nullable=False)
    idp_sso_url: Mapped[str] = mapped_column(String(2048), nullable=False)
    signing_certificates: Mapped[list[str]] = mapped_column(JSON, nullable=False)
    attribute_mappings: Mapped[dict] = mapped_column(
        JSON, nullable=False, default=dict, server_default="{}"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    organization: Mapped[Organization] = relationship(back_populates="saml_configuration")


class SamlAuthTransaction(Base):
    __tablename__ = "saml_auth_transactions"
    __table_args__ = (
        ForeignKeyConstraint(
            ["configuration_id", "organization_id"],
            [
                "organization_saml_configurations.id",
                "organization_saml_configurations.organization_id",
            ],
            ondelete="CASCADE",
        ),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    relay_state: Mapped[str] = mapped_column(String(128), nullable=False, unique=True, index=True)
    request_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    browser_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    configuration_digest: Mapped[str] = mapped_column(String(64), nullable=False)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    configuration_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organization_saml_configurations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    return_path: Mapped[str] = mapped_column(String(512), nullable=False, server_default="/")
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    consumed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class SamlAssertionReplay(Base):
    __tablename__ = "saml_assertion_replays"
    __table_args__ = (
        ForeignKeyConstraint(
            ["configuration_id", "organization_id"],
            [
                "organization_saml_configurations.id",
                "organization_saml_configurations.organization_id",
            ],
            ondelete="CASCADE",
        ),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    assertion_id: Mapped[str] = mapped_column(String(255), nullable=False, unique=True, index=True)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    configuration_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organization_saml_configurations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    consumed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class SamlIdentity(Base):
    __tablename__ = "saml_identities"
    __table_args__ = (
        UniqueConstraint("configuration_id", "subject"),
        UniqueConstraint("user_id", "configuration_id"),
    )
    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    configuration_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organization_saml_configurations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    subject: Mapped[str] = mapped_column(String(1024), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class OrganizationInvitation(Base):
    __tablename__ = "organization_invitations"
    __table_args__ = (UniqueConstraint("organization_id", "invited_email", "status"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    invited_email: Mapped[str] = mapped_column(String(320), nullable=False, index=True)
    role: Mapped[OrganizationMemberRole] = mapped_column(
        Enum(
            OrganizationMemberRole,
            name="organization_invitation_role",
            values_callable=lambda roles: [role.value for role in roles],
        ),
        nullable=False,
    )
    status: Mapped[OrganizationInvitationStatus] = mapped_column(
        Enum(
            OrganizationInvitationStatus,
            name="organization_invitation_status",
            values_callable=lambda statuses: [status.value for status in statuses],
        ),
        nullable=False,
        default=OrganizationInvitationStatus.PENDING,
        server_default=OrganizationInvitationStatus.PENDING.value,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    invited_by_user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    accepted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    organization: Mapped[Organization] = relationship(back_populates="invitations")
    invited_by: Mapped[User] = relationship(foreign_keys=[invited_by_user_id])


class ApiCredential(Base):
    __tablename__ = "api_credentials"
    __table_args__ = (UniqueConstraint("organization_id", "label"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    label: Mapped[str] = mapped_column(String(100), nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    last_used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    organization: Mapped[Organization] = relationship(back_populates="api_credentials")


class WebhookEndpoint(Base):
    __tablename__ = "webhook_endpoints"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    url: Mapped[str] = mapped_column(String(2048), nullable=False)
    secret_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    secret_ciphertext: Mapped[str] = mapped_column(String(255), nullable=False)
    is_active: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true", index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    organization: Mapped[Organization] = relationship(back_populates="webhook_endpoints")
    deliveries: Mapped[list["WebhookDelivery"]] = relationship(
        back_populates="endpoint", cascade="all, delete-orphan"
    )


class WebhookDelivery(Base):
    __tablename__ = "webhook_deliveries"
    __table_args__ = (UniqueConstraint("endpoint_id", "event_id"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    endpoint_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("webhook_endpoints.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    event_id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), nullable=False)
    event_type: Mapped[str] = mapped_column(String(100), nullable=False)
    payload: Mapped[dict] = mapped_column(JSON, nullable=False)
    status: Mapped[WebhookDeliveryStatus] = mapped_column(
        String(16),
        nullable=False,
        default=WebhookDeliveryStatus.PENDING,
        server_default=WebhookDeliveryStatus.PENDING.value,
        index=True,
    )
    attempt_count: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    last_error: Mapped[str | None] = mapped_column(String(500), nullable=True)
    next_attempt_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    endpoint: Mapped[WebhookEndpoint] = relationship(back_populates="deliveries")


class OAuthIdentity(Base):
    __tablename__ = "oauth_identities"
    __table_args__ = (
        UniqueConstraint("provider", "subject"),
        UniqueConstraint("provider", "user_id"),
    )

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    provider: Mapped[str] = mapped_column(String(32), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    user: Mapped[User] = relationship(back_populates="oauth_identities")


class OAuthLoginCode(Base):
    __tablename__ = "oauth_login_codes"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    sso_organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="CASCADE"),
        nullable=True,
        index=True,
    )
    code_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    user: Mapped[User] = relationship(back_populates="oauth_login_codes")


class PasswordResetToken(Base):
    __tablename__ = "password_reset_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    user: Mapped[User] = relationship(back_populates="password_reset_tokens")


class TransactionalEmail(Base):
    __tablename__ = "transactional_emails"
    __table_args__ = (Index("ix_transactional_emails_due", "status", "next_attempt_at"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    idempotency_key: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("organizations.id", ondelete="CASCADE"), index=True
    )
    kind: Mapped[str] = mapped_column(String(32), nullable=False)
    related_token_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    encrypted_message: Mapped[str | None] = mapped_column(Text)
    status: Mapped[str] = mapped_column(String(16), nullable=False, default="pending")
    attempts: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    next_attempt_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    lease_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    claim_id: Mapped[uuid.UUID | None] = mapped_column(UUID(as_uuid=True))
    last_error: Mapped[str | None] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class EmailVerificationToken(Base):
    __tablename__ = "email_verification_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AccountReactivationToken(Base):
    __tablename__ = "account_reactivation_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    auth_version: Mapped[int] = mapped_column(Integer, nullable=False)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class AccountDeletionTombstone(Base):
    """Minimal non-identifying marker retained for audit and restore reconciliation."""

    __tablename__ = "account_deletion_tombstones"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=False, unique=True
    )
    completed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), index=True
    )


class EmailChangeToken(Base):
    __tablename__ = "email_change_tokens"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    user_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True
    )
    old_email: Mapped[str] = mapped_column(String(320), nullable=False)
    new_email: Mapped[str] = mapped_column(String(320), nullable=False)
    auth_version: Mapped[int] = mapped_column(Integer, nullable=False)
    token_hash: Mapped[str] = mapped_column(String(64), nullable=False, unique=True)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    used_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )


class Document(Base):
    __tablename__ = "documents"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    mime_type: Mapped[str] = mapped_column(String(255), nullable=False)
    file_size: Mapped[int] = mapped_column(BigInteger, nullable=False)
    checksum: Mapped[str] = mapped_column(String(64), nullable=False)
    storage_location: Mapped[str] = mapped_column(Text, nullable=False)
    owner_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    created_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    is_finalized: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=False, server_default="false", index=True
    )
    finalized_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    finalized_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    reopened_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    reopened_by_user_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="RESTRICT"), nullable=True, index=True
    )
    status: Mapped[DocumentStatus] = mapped_column(
        Enum(
            DocumentStatus,
            name="document_status",
            values_callable=lambda statuses: [status.value for status in statuses],
        ),
        nullable=False,
        default=DocumentStatus.UPLOADED,
        server_default=DocumentStatus.UPLOADED.value,
        index=True,
    )
    error_details: Mapped[str | None] = mapped_column(Text, nullable=True)
    extracted_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_analysis_status: Mapped[AIAnalysisStatus] = mapped_column(
        Enum(
            AIAnalysisStatus,
            name="ai_analysis_status",
            values_callable=lambda statuses: [status.value for status in statuses],
        ),
        nullable=False,
        default=AIAnalysisStatus.PENDING,
        server_default=AIAnalysisStatus.PENDING.value,
    )
    ai_analysis_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    ai_analyzed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        server_default=func.now(),
        onupdate=func.now(),
    )
    invoice_extraction: Mapped["InvoiceExtraction | None"] = relationship(
        back_populates="document", uselist=False, cascade="all, delete-orphan"
    )
    owner: Mapped[User | None] = relationship(back_populates="documents", foreign_keys=[owner_id])
    created_by_user: Mapped[User | None] = relationship(
        back_populates="created_documents", foreign_keys=[created_by_user_id]
    )
    finalized_by_user: Mapped[User | None] = relationship(foreign_keys=[finalized_by_user_id])
    reopened_by_user: Mapped[User | None] = relationship(foreign_keys=[reopened_by_user_id])
    organization: Mapped[Organization | None] = relationship(back_populates="documents")
    ai_findings: Mapped[list["AIFinding"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )
    processing_operations: Mapped[list["ProcessingOperation"]] = relationship(
        back_populates="document", cascade="all, delete-orphan"
    )

    def transition_to(self, new_status: DocumentStatus) -> None:
        allowed_transitions = {
            DocumentStatus.UPLOADED: {DocumentStatus.QUEUED},
            DocumentStatus.QUEUED: {DocumentStatus.PROCESSING},
            DocumentStatus.PROCESSING: {
                DocumentStatus.COMPLETED,
                DocumentStatus.FAILED,
                DocumentStatus.QUEUED,
            },
            DocumentStatus.COMPLETED: {DocumentStatus.QUEUED},
            DocumentStatus.FAILED: {DocumentStatus.QUEUED},
        }
        if new_status not in allowed_transitions[self.status]:
            raise InvalidDocumentStatusTransition(
                f"Cannot transition document from {self.status.value} to {new_status.value}."
            )
        self.status = new_status


class ProcessingOperation(Base):
    __tablename__ = "processing_operations"

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    organization_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    status: Mapped[ProcessingOperationStatus] = mapped_column(
        Enum(
            ProcessingOperationStatus,
            name="processing_operation_status",
            values_callable=lambda statuses: [status.value for status in statuses],
        ),
        nullable=False,
        default=ProcessingOperationStatus.QUEUED,
        server_default=ProcessingOperationStatus.QUEUED.value,
        index=True,
    )
    priority: Mapped[ProcessingPriority] = mapped_column(
        String(length=16),
        nullable=False,
        default=ProcessingPriority.NORMAL,
        server_default=ProcessingPriority.NORMAL.value,
        index=True,
    )
    page_count: Mapped[int] = mapped_column(Integer, nullable=False, default=1, server_default="1")
    retry_attempts: Mapped[int] = mapped_column(
        Integer, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    error_details: Mapped[str | None] = mapped_column(Text, nullable=True)
    document: Mapped[Document] = relationship(back_populates="processing_operations")
    organization: Mapped[Organization | None] = relationship(back_populates="processing_operations")


class OrganizationMonthlyUsage(Base):
    __tablename__ = "organization_monthly_usage"
    __table_args__ = (UniqueConstraint("organization_id", "usage_month"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    organization_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("organizations.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    usage_month: Mapped[date] = mapped_column(Date, nullable=False, index=True)
    successful_processing_count: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    successful_processing_pages: Mapped[int] = mapped_column(
        BigInteger, nullable=False, default=0, server_default="0"
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )


class InvoiceExtraction(Base):
    __tablename__ = "invoice_extractions"

    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        primary_key=True,
    )
    invoice_number: Mapped[str | None] = mapped_column(String(255), nullable=True)
    invoice_date: Mapped[date | None] = mapped_column(Date, nullable=True)
    vendor_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    customer_name: Mapped[str | None] = mapped_column(String(255), nullable=True)
    currency: Mapped[str | None] = mapped_column(String(3), nullable=True)
    subtotal: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    tax: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    total: Mapped[Decimal | None] = mapped_column(Numeric(18, 2), nullable=True)
    is_valid: Mapped[bool] = mapped_column(
        Boolean, nullable=False, default=True, server_default="true"
    )
    validation_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    document: Mapped[Document] = relationship(back_populates="invoice_extraction")
    line_items: Mapped[list["InvoiceLineItem"]] = relationship(
        back_populates="invoice_extraction", cascade="all, delete-orphan"
    )


class InvoiceLineItem(Base):
    __tablename__ = "invoice_line_items"
    __table_args__ = (UniqueConstraint("invoice_document_id", "position"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    invoice_document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("invoice_extractions.document_id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    description: Mapped[str] = mapped_column(String(500), nullable=False)
    quantity: Mapped[Decimal] = mapped_column(Numeric(18, 4), nullable=False)
    unit_price: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    line_total: Mapped[Decimal] = mapped_column(Numeric(18, 2), nullable=False)
    invoice_extraction: Mapped[InvoiceExtraction] = relationship(back_populates="line_items")


class AIFinding(Base):
    __tablename__ = "ai_findings"
    __table_args__ = (UniqueConstraint("document_id", "signature"),)

    id: Mapped[uuid.UUID] = mapped_column(UUID(as_uuid=True), primary_key=True, default=uuid.uuid4)
    document_id: Mapped[uuid.UUID] = mapped_column(
        UUID(as_uuid=True),
        ForeignKey("documents.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    category: Mapped[AIFindingCategory] = mapped_column(
        Enum(
            AIFindingCategory,
            name="ai_finding_category",
            values_callable=lambda categories: [category.value for category in categories],
        ),
        nullable=False,
        index=True,
    )
    severity: Mapped[AIFindingSeverity] = mapped_column(
        Enum(
            AIFindingSeverity,
            name="ai_finding_severity",
            values_callable=lambda severities: [severity.value for severity in severities],
        ),
        nullable=False,
        index=True,
    )
    status: Mapped[AIFindingStatus] = mapped_column(
        Enum(
            AIFindingStatus,
            name="ai_finding_status",
            values_callable=lambda statuses: [status.value for status in statuses],
        ),
        nullable=False,
        default=AIFindingStatus.OPEN,
        server_default=AIFindingStatus.OPEN.value,
        index=True,
    )
    signature: Mapped[str] = mapped_column(String(160), nullable=False)
    title: Mapped[str] = mapped_column(String(255), nullable=False)
    explanation: Mapped[str] = mapped_column(Text, nullable=False)
    evidence: Mapped[dict[str, object]] = mapped_column(JSON, nullable=False, default=dict)
    affected_fields: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    confidence: Mapped[Decimal | None] = mapped_column(Numeric(5, 4), nullable=True)
    observed_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    expected_value: Mapped[str | None] = mapped_column(String(255), nullable=True)
    reviewed_by_id: Mapped[uuid.UUID | None] = mapped_column(
        UUID(as_uuid=True), ForeignKey("users.id", ondelete="SET NULL"), nullable=True
    )
    reviewed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, server_default=func.now(), onupdate=func.now()
    )
    document: Mapped[Document] = relationship(back_populates="ai_findings")
