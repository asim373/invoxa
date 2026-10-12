# Enterprise security controls (Step 6.4)

## Mandatory SSO

An OWNER enables enforcement with `PUT /organizations/security/sso` only after
the organization has an enabled, usable SAML configuration and SAML entitlement.
The response includes a random one-time owner recovery code only when enforcement
is first enabled (or when an OWNER explicitly rotates it while still able to
use SAML). Store that code offline. Only its SHA-256 digest is persisted.

Every workspace-bound request requires an active member and, while enforcement
is enabled, a valid SAML-authenticated session scoped to that same organization.
Password and social login sessions remain valid for other use but cannot access
the enforced workspace. Requests without an explicit organization selector are
also denied to avoid falling through legacy workspace behavior. SAML cannot be
disabled while enforcement is on. The one-use recovery endpoint requires an
active OWNER, a currently valid application session and the recovery code; it
only disables enforcement, does not issue a session or grant workspace access,
and is rate-limited and audited. A bad recovery attempt never changes policy.

## Sessions

Access tokens remain short-lived JWTs (configured expiry, 60 minutes by
default) and now carry a random session ID. The server stores session metadata,
not the bearer token. Logout, single-session revocation, and revoking all other
sessions take effect on the next request. Users can list/revoke their own
sessions; workspace OWNER/ADMIN can revoke an eligible member's session within
that workspace. Password changes/resets and administrative resets continue to
increment `auth_version` and revoke all of the user's sessions. Expired,
revoked, inactive-user, and stale-auth-version sessions are rejected.

## Secrets and audit

In production, invitations and administrative password-reset links are sent
through configured SMTP and their bearer secrets are not returned in API
responses. Failed delivery safely invalidates the newly issued invitation or
reset token. Development/test may return invitation tokens and temporary
passwords only when `APP_ENVIRONMENT` is explicitly `development` or `test`;
staging and production require configured mail delivery. These values are
never logged.

Organization security events are written in the same database transaction as
the administration action. The audit API is organization-scoped and available
to OWNER/ADMIN. Event context is allowlisted and excludes credentials, tokens,
passwords, SAML payloads, and certificate material. SCIM resource lifecycle
events continue to use the existing SCIM provisioning-event table; these new
events cover SCIM configuration and credential administration only.

## Redis outage behavior

Rate-limited authentication, password reset, Google authentication, SAML,
SCIM, invitation/team security actions, credential management, session
revocation, and SSO recovery fail closed with HTTP 503 and `Retry-After` when
Redis is unavailable. This policy is scoped at those call sites rather than
changing the global limiter default. Uploads and external read API traffic
retain their availability-first fail-open behavior; readiness separately
reports Redis unavailable.

## Schema

Migration `0029` adds organization SSO enforcement/recovery fields, persisted
user sessions, and tenant-scoped organization security audit events. It does not
alter SAML signature, audience, destination, issuer, time, or replay validation.
