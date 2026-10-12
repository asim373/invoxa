import { useState } from "react";

type BillingPeriod = "monthly" | "annual";

const comparison = [
  { feature: "Invoice OCR & header line-item extraction", standard: true, professional: true, enterprise: true },
  { feature: "Document management & search", standard: true, professional: true, enterprise: true },
  { feature: "CSV, JSON & XLSX exports", standard: true, professional: true, enterprise: true },
  { feature: "Financial validation", standard: true, professional: true, enterprise: true },
  { feature: "Basic anomaly detection", standard: true, professional: true, enterprise: true },
  { feature: "Standard email support", standard: true, professional: true, enterprise: true },
  { feature: "Advanced anomaly detection", standard: false, professional: true, enterprise: true },
  { feature: "Read-only document-list API", standard: false, professional: true, enterprise: true },
  { feature: "Webhooks", standard: false, professional: true, enterprise: true },
  { feature: "Team collaboration & roles", standard: false, professional: true, enterprise: true },
  { feature: "Priority processing", standard: false, professional: true, enterprise: true },
  { feature: "Priority email support", standard: false, professional: true, enterprise: true },
  { feature: "SAML SSO", standard: false, professional: false, enterprise: true },
  { feature: "SCIM user provisioning", standard: false, professional: false, enterprise: true },
  { feature: "Enterprise security controls", standard: false, professional: false, enterprise: true },
  { feature: "Custom volume quotas", standard: false, professional: false, enterprise: true },
  { feature: "On-premises / private cloud deployment", standard: false, professional: false, enterprise: true },
  { feature: "Dedicated priority support & SLA", standard: false, professional: false, enterprise: true },
  { feature: "Custom integrations", standard: false, professional: false, enterprise: true },
];

function Availability({ available }: { available: boolean }) {
  return (
    <span className={available ? "pricing-check" : "pricing-unavailable"}>
      <span aria-hidden="true">{available ? "✓" : "—"}</span>
      <span className="sr-only">{available ? "Included" : "Not included"}</span>
    </span>
  );
}

export function PricingPage() {
  const [period, setPeriod] = useState<BillingPeriod>("monthly");
  const annual = period === "annual";

  return (
    <main className="pricing-page">
      <header className="pricing-header">
        <a className="pricing-brand" href="/" aria-label="Invoxa home">
          <img src="/invoxa-logo.jpg" alt="Invoxa" />
        </a>
        <a className="pricing-signin" href="/">Sign in</a>
      </header>

      <section className="pricing-hero" aria-labelledby="pricing-title">
        <p className="eyebrow">Plans for confident finance operations</p>
        <h1 id="pricing-title">Clear limits. Room to grow.</h1>
        <p>Choose the processing capacity and collaboration your team needs.</p>
        <div className="pricing-period" role="group" aria-label="Billing period">
          <button
            type="button"
            aria-pressed={period === "monthly"}
            className={period === "monthly" ? "selected" : ""}
            onClick={() => setPeriod("monthly")}
          >
            Monthly
          </button>
          <button
            type="button"
            aria-pressed={period === "annual"}
            className={period === "annual" ? "selected" : ""}
            onClick={() => setPeriod("annual")}
          >
            Annual
          </button>
        </div>
      </section>

      <section className="pricing-cards" aria-label="Available plans">
        <article className="pricing-card">
          <div className="pricing-card-heading">
            <div><p className="eyebrow">For individuals</p><h2>Standard</h2></div>
          </div>
          <p className="pricing-summary">Core invoice processing for a focused workflow.</p>
          <p className="pricing-price"><strong>${annual ? "990" : "99"}</strong><span>/{annual ? "year" : "month"}</span></p>
          {annual && <p className="pricing-savings">Save $198/year</p>}
          <p className="pricing-capacity">1,000 pages / month</p>
          <dl className="pricing-limits">
            <div><dt>Storage</dt><dd>2 GB</dd></div>
            <div><dt>Seats</dt><dd>1 active seat</dd></div>
          </dl>
          <a className="pricing-primary" href="/?signup=1">Create a workspace</a>
          <p className="pricing-availability">Account creation is available. Billing is not handled in-app.</p>
        </article>

        <article className="pricing-card pricing-card-featured">
          <div className="pricing-card-heading">
            <div><p className="eyebrow">For growing teams</p><h2>Professional</h2></div>
            <span className="pricing-badge">Team plan</span>
          </div>
          <p className="pricing-summary">Everything in Standard, plus more capacity, team collaboration and workflow automation.</p>
          <p className="pricing-price"><strong>${annual ? "2,490" : "249"}</strong><span>/{annual ? "year" : "month"}</span></p>
          {annual && <p className="pricing-savings">Save $498/year</p>}
          <p className="pricing-capacity">5,000 pages / month</p>
          <dl className="pricing-limits">
            <div><dt>Storage</dt><dd>10 GB</dd></div>
            <div><dt>Seats</dt><dd>5 active seats</dd></div>
          </dl>
          <a className="pricing-primary" href="/?signup=1" aria-describedby="professional-status">
            Create account on Standard
          </a>
          <p className="pricing-availability" id="professional-status">Professional plan selection and billing are not available yet; signup starts on Standard.</p>
        </article>

        <article className="pricing-card pricing-card-enterprise">
          <div className="pricing-card-heading">
            <div><p className="eyebrow">For larger organizations</p><h2>Enterprise</h2></div>
          </div>
          <p className="pricing-summary">Everything in Professional, plus advanced identity, provisioning and security controls.</p>
          <p className="pricing-custom-price"><strong>Custom Pricing</strong><span>Contact Sales</span></p>
          <p className="pricing-capacity">5,001+ pages / month</p>
          <dl className="pricing-limits">
            <div><dt>Storage</dt><dd>11 GB+</dd></div>
            <div><dt>Seats</dt><dd>6+ active seats</dd></div>
          </dl>
          <button className="pricing-secondary" type="button" disabled aria-describedby="enterprise-status">
            Contact sales unavailable
          </button>
          <p className="pricing-availability" id="enterprise-status">Sales contact is not configured. No request was sent.</p>
        </article>
      </section>

      <p className="pricing-api-note">
        Professional API access currently supports read-only document listing. API upload, individual
        result retrieval and export are not available.
      </p>

      <section className="pricing-comparison" aria-labelledby="comparison-heading">
        <div className="pricing-section-heading">
          <div><p className="eyebrow">Compare plans</p><h2 id="comparison-heading">Included capabilities</h2></div>
          <p>Limits shown are the approved plan allowances.</p>
        </div>
        <div className="pricing-table-wrap">
          <table>
            <thead><tr><th scope="col">Capability</th><th scope="col">Standard</th><th scope="col">Professional</th><th scope="col">Enterprise</th></tr></thead>
            <tbody>
              {comparison.map((row) => (
                <tr key={row.feature}>
                  <th scope="row">{row.feature}</th>
                  <td><Availability available={row.standard} /></td>
                  <td><Availability available={row.professional} /></td>
                  <td><Availability available={row.enterprise} /></td>
                </tr>
              ))}
            </tbody>
          </table>
        </div>
        <p className="pricing-footnote">Enterprise processing limits, storage, and team capacity are tailored to your organization's requirements.</p>
      </section>
      <footer className="pricing-footer"><span>Invoxa</span><a href="/">Back to sign in</a></footer>
    </main>
  );
}
