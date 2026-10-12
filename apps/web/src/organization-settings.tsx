import { useEffect, useState } from "react";

import { apiRequest } from "./api";

type OrganizationProfileData = { id: string; name: string; slug: string };

export function OrganizationSettings({ canEdit }: { canEdit: boolean }) {
  const [profile, setProfile] = useState<OrganizationProfileData | null>(null);
  const [name, setName] = useState("");
  const [loading, setLoading] = useState(true);
  const [saving, setSaving] = useState(false);
  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  useEffect(() => {
    let active = true;
    void apiRequest<OrganizationProfileData>("/organizations/current")
      .then((data) => {
        if (!active) return;
        setProfile(data);
        setName(data.name);
      })
      .catch((requestError: unknown) => {
        if (active) {
          setError(
            requestError instanceof Error
              ? requestError.message
              : "Workspace information could not be loaded.",
          );
        }
      })
      .finally(() => {
        if (active) setLoading(false);
      });
    return () => {
      active = false;
    };
  }, []);

  async function saveProfile(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!canEdit || !profile) return;
    const trimmedName = name.trim();
    if (!trimmedName || trimmedName.length > 255) {
      setError("Enter a workspace name between 1 and 255 characters.");
      setSuccess("");
      return;
    }
    setSaving(true);
    setError("");
    setSuccess("");
    try {
      const updated = await apiRequest<OrganizationProfileData>("/organizations/current", {
        method: "PATCH",
        body: JSON.stringify({ name: trimmedName }),
      });
      setProfile(updated);
      setName(updated.name);
      setSuccess("Company profile saved.");
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "Workspace information could not be saved.",
      );
    } finally {
      setSaving(false);
    }
  }

  if (loading) return <section className="company-settings compact-state" role="status">Loading company profile...</section>;
  if (!profile) {
    return (
      <section className="company-settings" aria-labelledby="company-settings-heading">
        <h2 id="company-settings-heading">Company profile</h2>
        <div className="alert error" role="alert">{error || "Workspace information is unavailable."}</div>
      </section>
    );
  }

  return (
    <section className="company-settings" aria-labelledby="company-settings-heading">
      <div className="company-settings-card">
        <h2 id="company-settings-heading">Company profile</h2>
        <p className="company-settings-help">
          Manage the display name for this workspace. Its sign-in identifier stays unchanged.
        </p>
        {error && <div className="alert error" role="alert">{error}</div>}
        {success && <div className="alert success" role="status">{success}</div>}
        <form onSubmit={(event) => void saveProfile(event)}>
          <label htmlFor="company-profile-name">Company or workspace name</label>
          <input
            id="company-profile-name"
            name="company-name"
            value={name}
            maxLength={255}
            minLength={1}
            required
            readOnly={!canEdit}
            onChange={(event) => setName(event.target.value)}
          />
          <label htmlFor="company-profile-slug">Workspace sign-in identifier</label>
          <input id="company-profile-slug" value={profile.slug} readOnly />
          {canEdit && (
            <button
              className="primary-button"
              type="submit"
              disabled={saving || name.trim() === profile.name}
            >
              {saving ? "Saving..." : "Save company profile"}
            </button>
          )}
          {!canEdit && <p className="company-settings-help">Only workspace owners and admins can edit company details.</p>}
        </form>
      </div>
    </section>
  );
}
