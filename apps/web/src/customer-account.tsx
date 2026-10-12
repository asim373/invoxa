import { useEffect, useState } from "react";
import { apiRequest, clearStoredToken } from "./api";

type Profile = { display_name: string; email: string; email_verified: boolean; identity_managed: boolean };

export function EmailChangeConfirmation() {
  const [token] = useState(() => new URLSearchParams(window.location.search).get("token") ?? "");
  const [message, setMessage] = useState("");
  const [busy, setBusy] = useState(false);
  const [complete, setComplete] = useState(false);
  useEffect(() => { window.history.replaceState({}, "", "/confirm-email-change"); }, []);
  return <main className="auth-page"><section className="auth-card">
    <img className="auth-logo" src="/invoxa-logo.jpg" alt="Invoxa" />
    <h1>Confirm your new login email</h1>
    <p>Your current login email stays unchanged until you confirm. All sessions will be revoked.</p>
    {message && <p role={complete ? "status" : "alert"}>{message}</p>}
    {!complete && <button className="primary-button" disabled={busy || !token} onClick={async () => {
      setBusy(true);
      try {
        const response = await apiRequest<{detail: string}>("/auth/account/email-change/confirm", {
          method: "POST", body: JSON.stringify({token}),
        });
        clearStoredToken(); setComplete(true); setMessage(response.detail);
      } catch (error) { setMessage(error instanceof Error ? error.message : "Verification unavailable."); }
      finally { setBusy(false); }
    }}>Confirm email change</button>}
    {!token && !message && <p role="alert">This link is invalid or has expired. Request a new link in account settings.</p>}
    <a href="/">Back to sign in</a>
  </section></main>;
}

export function CustomerAccount() {
  const [profile, setProfile] = useState<Profile | null>(null);
  const [name, setName] = useState("");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [nextPassword, setNextPassword] = useState("");
  const [deactivationConfirmation, setDeactivationConfirmation] = useState("");
  const [deletionConfirmation, setDeletionConfirmation] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const [deleted, setDeleted] = useState(false);
  useEffect(() => {
    let active = true;
    apiRequest<Profile>("/auth/account/profile").then(p => {
      if (active) { setProfile(p); setName(p.display_name); }
    }).catch(e => { if (active) setError(e instanceof Error ? e.message : "Account unavailable."); });
    return () => { active = false; };
  }, []);
  async function submit(path: string, body: object, signOut: "none" | "redirect" | "deleted" = "none") {
    setBusy(true); setError(""); setMessage("");
    try {
      const response = await apiRequest<{detail?: string}>(path, {
        method: path.endsWith("profile") ? "PATCH" : "POST", body: JSON.stringify(body),
      });
      setPassword(""); setNextPassword(""); setMessage(response.detail ?? "Profile saved.");
      if (signOut === "redirect") { clearStoredToken(); window.location.assign("/"); }
      if (signOut === "deleted") { clearStoredToken(); setDeleted(true); }
    } catch (e) { setError(e instanceof Error ? e.message : "Account update unavailable."); }
    finally { setBusy(false); }
  }
  if (deleted) return <section className="account-settings" aria-label="Account deleted">
    <div className="alert success" role="status">Your account was permanently deleted. Shared workspace records were preserved.</div>
    <a href="/">Back to sign in</a>
  </section>;
  return <section className="account-settings" aria-label="Account settings">
    {error && <div className="alert error" role="alert">{error}</div>}
    {message && <div className="alert success" role="status">{message}</div>}
    {profile && <>
      <form className="auth-form" onSubmit={e => { e.preventDefault(); void submit("/auth/account/profile", {display_name: name}); }}>
        <h2>Profile</h2><p>Login email: {profile.email}</p>
        <label>Display name<input value={name} maxLength={120} onChange={e => setName(e.target.value)} autoComplete="name" /></label>
        <button className="primary-button" disabled={busy}>Save profile</button>
      </form>
      {profile.identity_managed ? <p>Your login identity is managed by your identity provider or workspace. Contact its administrator to change it.</p> : <>
        <form className="auth-form" onSubmit={e => { e.preventDefault(); void submit("/auth/account/email-change", {new_email: email, current_password: password}); }}>
          <h2>Change login email</h2>
          <p>Verify the new address before it replaces your current email. Confirmation signs out all sessions. To resend, wait at least one minute and request a new link.</p>
          <label>New email<input type="email" required value={email} onChange={e => setEmail(e.target.value)} autoComplete="email" /></label>
          <label>Current password<input type="password" required value={password} onChange={e => setPassword(e.target.value)} autoComplete="current-password" /></label>
          <button className="primary-button" disabled={busy}>Send verification link</button>
        </form>
        <form className="auth-form" onSubmit={e => { e.preventDefault(); void submit("/auth/account/password", {current_password: password, new_password: nextPassword}, "redirect"); }}>
          <h2>Change password</h2>
          <label>Current password<input type="password" required value={password} onChange={e => setPassword(e.target.value)} autoComplete="current-password" /></label>
          <label>New password<input type="password" required minLength={12} maxLength={128} value={nextPassword} onChange={e => setNextPassword(e.target.value)} autoComplete="new-password" /></label>
          <button className="primary-button" disabled={busy}>Change password and sign out</button>
        </form>
      </>}
      <p>Account security notices are mandatory. Email delivery does not guarantee exactly-once delivery.</p>
      {!profile.identity_managed && <form className="auth-form" onSubmit={e => {
        e.preventDefault(); void submit("/auth/account/deactivate", {
          current_password: password, confirmation: deactivationConfirmation,
        }, "redirect");
      }}>
        <h2>Deactivate account</h2>
        <p>This signs you out everywhere and blocks access. Shared workspace documents and audit records remain intact. Another active OWNER is required for each workspace you own.</p>
        <label>Current password<input type="password" required value={password} onChange={e => setPassword(e.target.value)} autoComplete="current-password" /></label>
        <label>Type DEACTIVATE<input required value={deactivationConfirmation} onChange={e => setDeactivationConfirmation(e.target.value)} autoComplete="off" /></label>
        <button className="primary-button" disabled={busy || deactivationConfirmation !== "DEACTIVATE"}>Deactivate account</button>
        <a href="/reactivate-account">Account reactivation</a>
      </form>}
      {!profile.identity_managed && <form className="auth-form" aria-label="Permanent account deletion" onSubmit={e => {
        e.preventDefault(); void submit("/auth/account/delete", {
          current_password: password, confirmation: deletionConfirmation,
        }, "deleted");
      }}>
        <h2>Permanent account deletion</h2>
        <p>This permanently removes your login identity and signs you out everywhere. Shared company documents and required audit records remain. You cannot recover this account.</p>
        <p>Deletion is blocked until another active OWNER exists for every workspace you own.</p>
        <label>Current password<input type="password" required value={password} onChange={e => setPassword(e.target.value)} autoComplete="current-password" /></label>
        <label>Type DELETE<input required value={deletionConfirmation} onChange={e => setDeletionConfirmation(e.target.value)} autoComplete="off" /></label>
        <button className="primary-button" disabled={busy || deletionConfirmation !== "DELETE"}>Delete Account Permanently</button>
      </form>}
    </>}
  </section>;
}

export function AccountRecovery() {
  const [token] = useState(() => new URLSearchParams(window.location.search).get("token") ?? "");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [message, setMessage] = useState("");
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  useEffect(() => { window.history.replaceState({}, "", "/reactivate-account"); }, []);
  return <main className="auth-page"><section className="auth-card">
    <img className="auth-logo" src="/invoxa-logo.jpg" alt="Invoxa" />
    <h1>Reactivate your account</h1>
    <p>Recover a voluntarily deactivated password account. Workspace or administrator restrictions cannot be overridden.</p>
    <form className="auth-form" onSubmit={async e => {
      e.preventDefault(); setBusy(true); setError("");
      try {
        const response = await apiRequest<{detail: string}>(token ? "/auth/account/reactivate" : "/auth/account/reactivation-request", {
          method: "POST", body: JSON.stringify(token ? {token, current_password: password} : {email, current_password: password}),
        }); setPassword(""); setMessage(response.detail);
      } catch (e) { setError(e instanceof Error ? e.message : "Account recovery unavailable."); }
      finally { setBusy(false); }
    }}>
      {!token && <label>Email<input type="email" required value={email} onChange={e => setEmail(e.target.value)} autoComplete="email" /></label>}
      <label>Current password<input type="password" required value={password} onChange={e => setPassword(e.target.value)} autoComplete="current-password" /></label>
      {error && <p role="alert">{error}</p>}
      {message && <p role="status">{message}</p>}
      <button className="primary-button" disabled={busy}>{token ? "Confirm reactivation" : "Send recovery instructions"}</button>
      <a href="/">Back to sign in</a>
    </form>
  </section></main>;
}
