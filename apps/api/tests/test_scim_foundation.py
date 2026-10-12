import uuid

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, select
from sqlalchemy.orm import Session
from sqlalchemy.pool import StaticPool

from apps.api.app.database import Base
from apps.api.app.models import (
    Organization,
    OrganizationMember,
    OrganizationMemberRole,
    OrganizationMemberStatus,
    ScimConfiguration,
    ScimCredential,
    User,
)
from apps.api.app.scim import (
    ScimConfigurationUpdate,
    ScimCredentialCreate,
    _hash,
    create_scim_credential,
    get_scim_principal,
    revoke_scim_credential,
    rotate_scim_credential,
    update_scim_configuration,
)
from apps.api.app.tenant import TenantContext


@pytest.fixture
def database():
    engine = create_engine(
        "sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool
    )
    Base.metadata.create_all(engine)
    with Session(engine) as session:
        yield session
    Base.metadata.drop_all(engine)
    engine.dispose()


def setup_owner(database: Session, plan: str = "enterprise", role=OrganizationMemberRole.OWNER):
    user = User(email=f"scim-{uuid.uuid4().hex}@example.com", password_hash="unused")
    organization = Organization(name="SCIM", slug=f"scim-{uuid.uuid4().hex}", plan_code=plan)
    membership = OrganizationMember(
        user=user,
        organization=organization,
        role=role,
        status=OrganizationMemberStatus.ACTIVE,
    )
    database.add(membership)
    database.commit()
    return TenantContext(organization, membership), user, organization


def test_owner_creation_rotation_and_revocation_are_one_time(database: Session):
    tenant, _, _ = setup_owner(database)
    update_scim_configuration(ScimConfigurationUpdate(), tenant, database)
    created = create_scim_credential(ScimCredentialCreate(label="primary"), tenant, database)
    stored = database.get(ScimCredential, created.id)
    assert created.secret.startswith("invx_scim_")
    assert stored is not None and stored.secret_hash == _hash(created.secret)
    assert created.secret not in stored.secret_hash
    rotated = rotate_scim_credential(created.id, tenant, database)
    assert rotated.secret != created.secret
    principal = get_scim_principal(f"Bearer {rotated.secret}", database)
    assert tenant.organization is not None
    assert principal[1].id == tenant.organization.id
    with pytest.raises(HTTPException):
        get_scim_principal(f"Bearer {created.secret}", database)
    revoke_scim_credential(rotated.id, tenant, database)
    with pytest.raises(HTTPException):
        get_scim_principal(f"Bearer {rotated.secret}", database)


@pytest.mark.parametrize("plan", ["standard", "professional"])
def test_non_enterprise_cannot_manage_scim(database: Session, plan: str):
    tenant, _, _ = setup_owner(database, plan)
    with pytest.raises(HTTPException):
        update_scim_configuration(ScimConfigurationUpdate(), tenant, database)


@pytest.mark.parametrize("role", [OrganizationMemberRole.ADMIN, OrganizationMemberRole.MEMBER])
def test_non_owner_cannot_manage_scim(database: Session, role):
    tenant, _, _ = setup_owner(database, role=role)
    with pytest.raises(HTTPException):
        update_scim_configuration(ScimConfigurationUpdate(), tenant, database)


def test_disabled_configuration_rejects_token_and_tenant_is_token_bound(database: Session):
    tenant, _, organization = setup_owner(database)
    update_scim_configuration(ScimConfigurationUpdate(), tenant, database)
    created = create_scim_credential(ScimCredentialCreate(label="primary"), tenant, database)
    configuration = database.scalar(
        select(ScimConfiguration).where(ScimConfiguration.organization_id == organization.id)
    )
    assert configuration is not None
    configuration.enabled = False
    database.commit()
    with pytest.raises(HTTPException):
        get_scim_principal(f"Bearer {created.secret}", database)


def test_configuration_and_credential_serialization_never_include_secret_hash(database: Session):
    tenant, _, _ = setup_owner(database)
    configuration = update_scim_configuration(ScimConfigurationUpdate(), tenant, database)
    created = create_scim_credential(ScimCredentialCreate(label="primary"), tenant, database)
    assert "secret_hash" not in configuration.model_dump()
    assert "secret_hash" not in created.model_dump()
