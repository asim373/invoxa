import React, { useEffect, useRef, useState } from "react";
import ReactDOM from "react-dom/client";
import "./styles.css";
import { PricingPage } from "./pricing";
import { AccountRecovery, CustomerAccount, EmailChangeConfirmation } from "./customer-account";
import { OrganizationSettings } from "./organization-settings";
import {
  AUTHENTICATION_LOST_EVENT,
  API_BASE_URL,
  AuthenticationLostError,
  apiRequest,
  authenticatedFetch,
  clearStoredToken,
  getStoredToken,
  PUBLIC_APP_ORIGIN,
  setStoredOrganizationId,
  setStoredToken,
} from "./api";

type DocumentStatus = "uploaded" | "queued" | "processing" | "completed" | "failed";

type User = {
  id: string;
  email: string;
  is_active: boolean;
  role: "admin" | "reviewer" | "viewer";
  created_at: string;
  updated_at: string;
  organization_id?: string | null;
  organization_name?: string | null;
  membership_role?: TeamRole | null;
  force_password_change?: boolean;
};

type TeamMember = {
  id: string;
  user_id: string;
  email: string;
  role: "owner" | "admin" | "member" | "reviewer" | "viewer";
  status: string;
  joined_at: string | null;
};

type TeamInvitation = {
  id: string;
  invited_email: string;
  role: "owner" | "admin" | "member" | "reviewer" | "viewer";
  status: string;
  expires_at: string;
  invitation_token?: string;
};

type InvitationPreview = {
  organization_id: string;
  organization_name: string;
  invited_email: string;
  role: TeamRole;
  expires_at: string;
};

type TeamRole = "owner" | "admin" | "member" | "reviewer" | "viewer";

function teamRoleLabel(role: TeamRole): string {
  const normalizedRole = role.toLowerCase() as TeamRole;
  if (normalizedRole === "owner") return "Owner";
  if (normalizedRole === "admin") return "Admin";
  if (normalizedRole === "member") return "Team Member";
  return normalizedRole === "reviewer" ? "Legacy Reviewer" : "Legacy Viewer";
}

function teamErrorMessage(error: unknown): string {
  const message =
    error instanceof Error ? error.message : "Something went wrong. Please try again.";
  if (message.includes("pending invitation"))
    return "An invitation for this email is already pending.";
  if (message.includes("already a member"))
    return "This person is already a member of the workspace.";
  if (message.includes("active seat limit")) return message;
  if (message.includes("permission") || message.includes("403"))
    return "You don't have permission to perform this action.";
  if (message.includes("not found"))
    return "That team record is no longer available. Refresh and try again.";
  return message;
}

function invitationErrorMessage(error: unknown): string {
  const message = error instanceof Error ? error.message : "This invitation is no longer valid.";
  if (message.includes("email does not match"))
    return "This invitation was sent to another email address. Sign in with the invited account.";
  if (message.includes("expired"))
    return "This invitation has expired. Ask the workspace administrator for a new invitation.";
  if (message.includes("no longer available") || message.includes("invalid"))
    return "This invitation is no longer valid.";
  return message;
}

type DocumentItem = {
  id: string;
  original_filename: string;
  mime_type: string;
  file_size: number;
  status: DocumentStatus;
  created_at: string;
  updated_at: string;
};

type DocumentDetail = DocumentItem & {
  extracted_text: string | null;
  error_details: string | null;
  invoice_extraction: InvoiceExtraction | null;
};

type InvoiceLineItem = {
  position: number;
  description: string;
  quantity: string;
  unit_price: string;
  line_total: string;
};

type InvoiceExtraction = {
  invoice_number: string | null;
  invoice_date: string | null;
  vendor_name: string | null;
  customer_name: string | null;
  currency: string | null;
  subtotal: string | null;
  tax: string | null;
  total: string | null;
  is_valid: boolean;
  validation_error: string | null;
  line_items: InvoiceLineItem[];
};

type PaginatedDocuments = {
  items: DocumentItem[];
  total: number;
  page: number;
  page_size: number;
  total_pages: number;
};

type AIFinding = {
  id: string;
  document_id: string;
  document_filename: string;
  invoice_number: string | null;
  vendor_name: string | null;
  category: string;
  severity: "low" | "medium" | "high" | "critical";
  status: "open" | "acknowledged" | "resolved";
  title: string;
  explanation: string;
  evidence: Record<string, unknown>;
  affected_fields: string[];
  confidence: string | null;
  observed_value: string | null;
  expected_value: string | null;
  created_at: string;
};

type DocumentAnalysis = {
  document_id: string;
  analysis_status: "pending" | "completed" | "failed";
  analysis_error: string | null;
  analyzed_at: string | null;
  risk_level: "clear" | "review" | "high";
  summary: string;
  counts: Record<string, number>;
  financial_validation_status: "PASSED" | "WARNING" | "FAILED";
  financial_validation_counts: Record<string, number>;
  findings: AIFinding[];
};

type CurrencyTotal = { currency: string; total: string; tax: string; average: string };
type Analytics = {
  kpis: {
    total_documents: number;
    completed_documents: number;
    total_invoices: number;
    currency_totals: CurrencyTotal[];
    needs_review: number;
    ai_findings: number;
    unresolved_ai_findings: number;
    high_severity_ai_findings: number;
    validation_issue_count: number;
  };
  trends: {
    invoice_spend: { period: string; currency: string; total: string }[];
    invoice_count: { period: string; count: number }[];
    top_vendors: { vendor: string; currency: string; total: string }[];
    document_status: Record<string, number>;
    validation_status: Record<string, number>;
    findings_by_severity: Record<string, number>;
    findings_over_time: Record<string, number>;
  };
};

type ReportType = "financial" | "ai-analysis" | "processing-quality";
type WorkspaceView = "overview" | "ai-analysis" | "reports" | "documents" | "company" | "team" | "usage" | "account";

type UsageSnapshot = {
  plan: { code: string; name: string };
  processing: {
    period: string;
    used_pages: number;
    limit_pages: number;
    remaining_pages: number;
  };
  seats: { used: number; limit: number; remaining: number };
  storage: { used_bytes: number; limit_bytes: number; remaining_bytes: number };
};

type AuthMode = "login" | "register" | "forgot" | "reset" | "google_onboarding";

export const DOCUMENT_POLL_INTERVAL_MS = 1500;

function formatFileSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
}

function formatUsageBytes(bytes: number) {
  const value = Math.max(0, bytes);
  if (value < 1000) return `${value} B`;
  const units = ["KB", "MB", "GB", "TB"];
  let scaled = value;
  let unit = units[0];
  for (const candidate of units) {
    scaled /= 1000;
    unit = candidate;
    if (scaled < 1000 || candidate === units[units.length - 1]) break;
  }
  return `${scaled >= 10 ? scaled.toFixed(0) : scaled.toFixed(1)} ${unit}`;
}

function formatUsagePeriod(period: string) {
  const date = new Date(`${period}-01T00:00:00Z`);
  return Number.isNaN(date.getTime())
    ? period
    : date.toLocaleDateString(undefined, { month: "long", year: "numeric", timeZone: "UTC" });
}

const FINANCIAL_FINDING_CATEGORIES = new Set([
  "arithmetic_anomaly",
  "missing_information",
  "tax_consistency",
  "date_anomaly",
  "low_confidence_extraction",
]);

function findingCategoryLabel(category: string) {
  return category.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function findingDisplayLabel(finding: AIFinding) {
  if (finding.category === "duplicate_invoice") return "Possible duplicate";
  if (finding.category === "unusual_amount") return "Unusual amount";
  if (finding.category === "vendor_pattern") return "Vendor pattern";
  return findingCategoryLabel(finding.category);
}

function isUsageSnapshot(value: unknown): value is UsageSnapshot {
  if (!value || typeof value !== "object") return false;
  const snapshot = value as Record<string, unknown>;
  const hasMetric = (metric: unknown, keys: string[]) =>
    !!metric &&
    typeof metric === "object" &&
    keys.every((key) => typeof (metric as Record<string, unknown>)[key] === "number");
  return (
    !!snapshot.plan &&
    typeof snapshot.plan === "object" &&
    typeof (snapshot.plan as Record<string, unknown>).name === "string" &&
    typeof (snapshot.plan as Record<string, unknown>).code === "string" &&
    hasMetric(snapshot.processing, ["used_pages", "limit_pages", "remaining_pages"]) &&
    typeof (snapshot.processing as Record<string, unknown>).period === "string" &&
    hasMetric(snapshot.seats, ["used", "limit", "remaining"]) &&
    hasMetric(snapshot.storage, ["used_bytes", "limit_bytes", "remaining_bytes"])
  );
}

function UsageMetric({
  label,
  used,
  limit,
  remaining,
  unit,
  formatValue = (value) => String(value),
}: {
  label: string;
  used: number;
  limit: number;
  remaining: number;
  unit?: string;
  formatValue?: (value: number) => string;
}) {
  const percentage =
    limit > 0 ? Math.min(100, Math.max(0, (used / limit) * 100)) : used > 0 ? 100 : 0;
  const atLimit = used >= limit && limit >= 0;
  return (
    <article className="usage-card">
      <div className="usage-card-heading">
        <h3>{label}</h3>
        {atLimit && <span className="usage-status">Limit reached</span>}
      </div>
      <p className="usage-value">
        <strong>{formatValue(used)}</strong>{" "}
        <span>
          of {formatValue(limit)}
          {unit ? ` ${unit}` : ""}
        </span>
      </p>
      <div className="usage-progress-track" aria-hidden="true">
        <span style={{ width: `${percentage}%` }} />
      </div>
      <p className="usage-remaining">
        {formatValue(Math.max(0, remaining))}
        {unit ? ` ${unit}` : ""} remaining
      </p>
    </article>
  );
}

function formatDate(value: string) {
  return new Date(value).toLocaleString();
}

function statusLabel(status: DocumentStatus) {
  return status.charAt(0).toUpperCase() + status.slice(1);
}

function displayValue(value: string | null | undefined) {
  return value?.trim() || "Not extracted";
}

function formatMoney(value: string | null, currency: string | null) {
  if (!value) return "Not extracted";
  const amount = Number(value);
  if (!Number.isFinite(amount)) return value;
  if (currency) {
    try {
      return new Intl.NumberFormat(undefined, { style: "currency", currency }).format(amount);
    } catch {
      // Keep a readable value when OCR returns an invalid currency code.
    }
  }
  return amount.toLocaleString(undefined, { minimumFractionDigits: 2, maximumFractionDigits: 2 });
}

function asRecord(value: unknown): Record<string, unknown> {
  return value !== null && typeof value === "object" && !Array.isArray(value)
    ? (value as Record<string, unknown>)
    : {};
}

function metricLabel(value: string) {
  return value.replaceAll("_", " ").replace(/\b\w/g, (letter) => letter.toUpperCase());
}

function ReportPreview({
  report,
  reportType,
}: {
  report: Record<string, unknown>;
  reportType: ReportType;
}) {
  const summary = asRecord(report.summary);
  const currencyTotals = Array.isArray(summary.currency_totals) ? summary.currency_totals : [];
  const title =
    reportType === "financial"
      ? "Financial Summary Report"
      : reportType === "ai-analysis"
        ? "AI Analysis Report"
        : "Processing Quality Report";
  const headlineMetrics = [
    ["Total documents", summary.total_documents],
    ["Completed", summary.completed_documents],
    ["Needs review", summary.needs_review],
    ["AI findings", summary.ai_findings],
  ] as const;
  const detailSource: Array<[string, unknown]> =
    reportType === "ai-analysis"
      ? [
          ["Findings by severity", report.findings_by_severity],
          ["Review status", report.review_status],
        ]
      : reportType === "processing-quality"
        ? [
            ["Processing status", report.document_status],
            ["Validation status", report.validation_status],
          ]
        : [["Top vendors", report.top_vendors]];

  return (
    <div className="structured-report">
      <h4>{title}</h4>
      <div className="report-metrics" aria-label="Report summary">
        {headlineMetrics.map(([label, value]) => (
          <div key={label}>
            <span>{label}</span>
            <strong>{typeof value === "number" || typeof value === "string" ? value : "—"}</strong>
          </div>
        ))}
      </div>
      {currencyTotals.length > 0 && (
        <div className="report-currency" aria-label="Financial summary">
          {currencyTotals.map((item, index) => {
            const currency = asRecord(item);
            const code = typeof currency.currency === "string" ? currency.currency : null;
            return (
              <div key={`${code ?? "currency"}-${index}`}>
                <strong>{code ?? "Currency"}</strong>
                <span>
                  Total{" "}
                  <b>
                    {formatMoney(typeof currency.total === "string" ? currency.total : null, code)}
                  </b>
                </span>
                <span>
                  Tax{" "}
                  <b>{formatMoney(typeof currency.tax === "string" ? currency.tax : null, code)}</b>
                </span>
                <span>
                  Average{" "}
                  <b>
                    {formatMoney(
                      typeof currency.average === "string" ? currency.average : null,
                      code,
                    )}
                  </b>
                </span>
              </div>
            );
          })}
        </div>
      )}
      <div className="report-breakdown">
        {detailSource.map(([heading, value]) => {
          const rows: Array<[string, string]> = Array.isArray(value)
            ? value.slice(0, 4).map((item) => {
                const row = asRecord(item);
                return [
                  String(row.vendor ?? row.period ?? row.filename ?? "Item"),
                  typeof row.total === "string"
                    ? formatMoney(row.total, typeof row.currency === "string" ? row.currency : null)
                    : String(row.count ?? row.findings ?? "—"),
                ];
              })
            : Object.entries(asRecord(value)).map(([label, item]) => [label, String(item)]);
          if (rows.length === 0) return null;
          return (
            <section key={heading}>
              <h5>{heading}</h5>
              {rows.map(([label, value]) => (
                <div className="report-row" key={label}>
                  <span>{metricLabel(label)}</span>
                  <strong>{value}</strong>
                </div>
              ))}
            </section>
          );
        })}
        {reportType === "processing-quality" && report.low_confidence_documents !== undefined && (
          <section>
            <h5>Extraction review</h5>
            <div className="report-row">
              <span>Low Confidence Documents</span>
              <strong>{String(report.low_confidence_documents)}</strong>
            </div>
          </section>
        )}
      </div>
      <details className="raw-report">
        <summary>View raw JSON</summary>
        <pre>{JSON.stringify(report, null, 2)}</pre>
      </details>
    </div>
  );
}

const workspaceLabels: Record<WorkspaceView, { title: string; description: string }> = {
  account: { title: "Account settings", description: "Manage your profile and login security." },
  overview: {
    title: "Overview",
    description: "Monitor invoice performance, review exposure, and business trends.",
  },
  "ai-analysis": {
    title: "AI Analysis",
    description: "Review explainable findings and documents that need attention.",
  },
  reports: {
    title: "Reports",
    description: "Generate financial, analysis, and processing-quality reports.",
  },
  documents: {
    title: "Documents",
    description: "Manage and analyze your documents and invoices.",
  },
  team: {
    title: "Team",
    description: "Manage members, roles, and workspace access.",
  },
  company: {
    title: "Company settings",
    description: "Manage your company profile and workspace identity.",
  },
  usage: {
    title: "Plan & Usage",
    description: "Review your workspace plan and current usage allowances.",
  },
};

function NavigationIcon({ view }: { view: WorkspaceView }) {
  const paths: Record<WorkspaceView, React.ReactNode> = {
    account: <><circle cx="12" cy="8" r="4" /><path d="M5 21v-2a7 7 0 0 1 14 0v2" /></>,
    overview: (
      <>
        <path d="M3 12 12 4l9 8" />
        <path d="M5 10v10h14V10M9 20v-6h6v6" />
      </>
    ),
    "ai-analysis": (
      <>
        <path d="M12 3v3M12 18v3M3 12h3M18 12h3" />
        <circle cx="12" cy="12" r="4" />
        <path d="m5.6 5.6 2.1 2.1m8.6 8.6 2.1 2.1m0-12.8-2.1 2.1m-8.6 8.6-2.1 2.1" />
      </>
    ),
    reports: (
      <>
        <path d="M6 3h9l3 3v15H6z" />
        <path d="M9 13h6M9 17h6M14 3v4h4" />
      </>
    ),
    documents: (
      <>
        <path d="M5 3h10l4 4v14H5z" />
        <path d="M14 3v5h5M8 13h8M8 17h6" />
      </>
    ),
    team: (
      <>
        <circle cx="9" cy="8" r="3" />
        <circle cx="17" cy="9" r="2.5" />
        <path d="M3 20c.8-3.1 2.8-4.7 6-4.7s5.2 1.6 6 4.7M15 15c3 .2 4.8 1.8 5.5 4.5" />
      </>
    ),
    company: (
      <>
        <path d="M3 21h18M5 21V7l7-4 7 4v14M9 21v-5h6v5M8 9h.01M12 9h.01M16 9h.01M8 12h.01M16 12h.01" />
      </>
    ),
    usage: (
      <>
        <path d="M4 19h16" />
        <path d="M6 16v-3M10 16V9M14 16V6M18 16v-5" />
      </>
    ),
  };
  return (
    <svg
      aria-hidden="true"
      className="nav-icon"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      {paths[view]}
    </svg>
  );
}

function GoogleMark() {
  return (
    <svg className="google-mark" aria-hidden="true" viewBox="0 0 18 18">
      <path
        fill="#4285f4"
        d="M17.64 9.205c0-.638-.057-1.252-.164-1.841H9v3.481h4.844a4.14 4.14 0 0 1-1.797 2.715v2.258h2.909c1.702-1.567 2.684-3.875 2.684-6.613Z"
      />
      <path
        fill="#34a853"
        d="M9 18c2.43 0 4.468-.806 5.956-2.182l-2.909-2.258c-.806.54-1.836.859-3.047.859-2.344 0-4.328-1.585-5.037-3.714H.956v2.332A9 9 0 0 0 9 18Z"
      />
      <path
        fill="#fbbc05"
        d="M3.963 10.705A5.41 5.41 0 0 1 3.682 9c0-.592.102-1.168.281-1.705V4.963H.956A9 9 0 0 0 0 9c0 1.452.347 2.827.956 4.037l3.007-2.332Z"
      />
      <path
        fill="#ea4335"
        d="M9 3.581c1.322 0 2.508.454 3.441 1.346l2.582-2.581C13.464.892 11.426 0 9 0A9 9 0 0 0 .956 4.963l3.007 2.332C4.672 5.166 6.656 3.581 9 3.581Z"
      />
    </svg>
  );
}

function CopyIcon() {
  return (
    <svg
      aria-hidden="true"
      className="copy-icon"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <rect x="8" y="8" width="11" height="11" rx="2" />
      <path d="M16 8V6a2 2 0 0 0-2-2H6a2 2 0 0 0-2 2v8a2 2 0 0 0 2 2h2" />
    </svg>
  );
}

function CheckIcon() {
  return (
    <svg
      aria-hidden="true"
      className="copy-icon"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth="2"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="m5 12 4 4L19 6" />
    </svg>
  );
}

function TeamAlertIcon() {
  return (
    <svg
      aria-hidden="true"
      className="team-alert-icon"
      data-testid="team-alert-icon"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <path d="M10.3 3.8 2.2 18a2 2 0 0 0 1.7 3h16.2a2 2 0 0 0 1.7-3L13.7 3.8a2 2 0 0 0-3.4 0Z" />
      <path d="M12 9v4M12 17h.01" />
    </svg>
  );
}

function TeamSuccessIcon() {
  return (
    <svg
      aria-hidden="true"
      className="team-alert-icon"
      data-testid="team-success-icon"
      fill="none"
      viewBox="0 0 24 24"
      stroke="currentColor"
      strokeWidth="1.8"
      strokeLinecap="round"
      strokeLinejoin="round"
    >
      <circle cx="12" cy="12" r="9" />
      <path d="m8 12 2.5 2.5L16 9" />
    </svg>
  );
}

export function App() {
  const [token, setToken] = useState(getStoredToken);
  const [user, setUser] = useState<User | null>(null);

  const inviteRoute = window.location.pathname === "/invite/accept";
  const pricingRoute = window.location.pathname === "/pricing";
  const resetToken = inviteRoute
    ? ""
    : (new URLSearchParams(window.location.search).get("token") ?? "");
  const invitationToken = inviteRoute
    ? (new URLSearchParams(window.location.search).get("token") ?? "")
    : "";
  const googleLoginCode = new URLSearchParams(window.location.search).get("google_code") ?? "";
  const googleAuthError = new URLSearchParams(window.location.search).get("google_error") ?? "";
  const samlCallback = new URLSearchParams(window.location.hash.slice(1));
  const samlLoginCode = samlCallback.get("saml_code") ?? "";
  const samlAuthError = samlCallback.get("saml_error") ?? "";
  const googleInvitation =
    new URLSearchParams(window.location.search).get("google_invitation") === "1";
  const googleInvitationOrganization =
    new URLSearchParams(window.location.search).get("organization_id") ?? "";
  const googleOnboardingToken =
    new URLSearchParams(window.location.search).get("google_onboarding") ?? "";
  const [authMode, setAuthMode] = useState<AuthMode>(
    resetToken
      ? "reset"
      : googleOnboardingToken
        ? "google_onboarding"
        : new URLSearchParams(window.location.search).get("signup") === "1"
          ? "register"
          : "login",
  );
  const [email, setEmail] = useState("");
  const [verificationPending, setVerificationPending] = useState(
    window.location.pathname === "/verify-email",
  );
  const [verificationToken, setVerificationToken] = useState(() => {
    const value = window.location.pathname === "/verify-email"
      ? new URLSearchParams(window.location.search).get("token") ?? "" : "";
    return value;
  });
  useEffect(() => {
    if (verificationToken) window.history.replaceState({}, "", "/verify-email");
  }, [verificationToken]);
  const [companyName, setCompanyName] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [passwordChangeRequired, setPasswordChangeRequired] = useState(false);
  const [showPassword, setShowPassword] = useState(false);
  const [googleEnabled, setGoogleEnabled] = useState(false);
  const [googleLoading, setGoogleLoading] = useState(Boolean(googleLoginCode));
  const [samlLoading, setSamlLoading] = useState(Boolean(samlLoginCode));
  const [samlEntryOpen, setSamlEntryOpen] = useState(false);
  const [samlOrganizationSlug, setSamlOrganizationSlug] = useState("");
  const [invitationPreview, setInvitationPreview] = useState<InvitationPreview | null>(null);
  const [invitationLoading, setInvitationLoading] = useState(Boolean(invitationToken));
  const [invitationError, setInvitationError] = useState("");
  const [invitationSuccess, setInvitationSuccess] = useState("");
  const [invitationMode, setInvitationMode] = useState<"choose" | "new" | "login">("choose");
  const [invitationPassword, setInvitationPassword] = useState("");
  const invitationAccepted = useRef(false);

  const [documents, setDocuments] = useState<DocumentItem[]>([]);
  const [documentTotal, setDocumentTotal] = useState(0);
  const [totalPages, setTotalPages] = useState(0);
  const [page, setPage] = useState(1);
  const [searchInput, setSearchInput] = useState("");
  const [search, setSearch] = useState("");
  const [statusFilter, setStatusFilter] = useState("");
  const [mimeFilter, setMimeFilter] = useState("");
  const [sortBy, setSortBy] = useState("created_at");
  const [sortDirection, setSortDirection] = useState("desc");
  const [selectedDocumentIds, setSelectedDocumentIds] = useState<Set<string>>(new Set());
  const [selectedDocument, setSelectedDocument] = useState<DocumentDetail | null>(null);
  const [documentAnalysis, setDocumentAnalysis] = useState<DocumentAnalysis | null>(null);
  const [documentAnalysisLoading, setDocumentAnalysisLoading] = useState(false);
  const [documentAnalysisError, setDocumentAnalysisError] = useState("");

  const [loading, setLoading] = useState(false);
  const [documentsLoading, setDocumentsLoading] = useState(false);
  const [uploading, setUploading] = useState(false);
  const [detailLoading, setDetailLoading] = useState(false);
  const [previewUrl, setPreviewUrl] = useState("");
  const [previewLoading, setPreviewLoading] = useState(false);
  const [previewError, setPreviewError] = useState("");
  const [exporting, setExporting] = useState<"csv" | "xlsx" | "json" | null>(null);
  const [workspaceView, setWorkspaceView] = useState<WorkspaceView>("documents");
  const [sidebarOpen, setSidebarOpen] = useState(false);
  const [analyticsRange, setAnalyticsRange] = useState("30d");
  const [customStartDate, setCustomStartDate] = useState("");
  const [customEndDate, setCustomEndDate] = useState("");
  const [analytics, setAnalytics] = useState<Analytics | null>(null);
  const [findings, setFindings] = useState<AIFinding[]>([]);
  const [insightsLoading, setInsightsLoading] = useState(false);
  const [insightsError, setInsightsError] = useState("");
  const [findingSeverity, setFindingSeverity] = useState("");
  const [findingStatus, setFindingStatus] = useState("open");
  const [selectedFinding, setSelectedFinding] = useState<AIFinding | null>(null);
  const [reportType, setReportType] = useState<ReportType>("financial");
  const [report, setReport] = useState<Record<string, unknown> | null>(null);
  const [reportLoading, setReportLoading] = useState(false);
  const [deleteTarget, setDeleteTarget] = useState<DocumentItem | null>(null);
  const [teamMembers, setTeamMembers] = useState<TeamMember[]>([]);
  const [teamInvitations, setTeamInvitations] = useState<TeamInvitation[]>([]);
  const [teamSearch, setTeamSearch] = useState("");
  const [teamLoading, setTeamLoading] = useState(false);
  const [inviteOpen, setInviteOpen] = useState(false);
  const [inviteEmail, setInviteEmail] = useState("");
  const [inviteRole, setInviteRole] = useState<"admin" | "member">("member");
  const [teamError, setTeamError] = useState("");
  const [teamSuccess, setTeamSuccess] = useState("");
  const [newLinkInvitationId, setNewLinkInvitationId] = useState<string | null>(null);
  const [copiedInvitationId, setCopiedInvitationId] = useState<string | null>(null);
  const [invitationLink, setInvitationLink] = useState<{
    id: string;
    email: string;
    url: string;
  } | null>(null);
  const [teamAction, setTeamAction] = useState<
    | { kind: "role"; member: TeamMember }
    | { kind: "deactivate"; member: TeamMember }
    | { kind: "reset"; member: TeamMember }
    | { kind: "revoke"; invitation: TeamInvitation }
    | null
  >(null);
  const [roleUpdate, setRoleUpdate] = useState<"admin" | "member">("member");
  const [teamActionLoading, setTeamActionLoading] = useState(false);
  const [usage, setUsage] = useState<UsageSnapshot | null>(null);
  const [usageLoading, setUsageLoading] = useState(false);
  const [usageError, setUsageError] = useState("");

  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const copyResetTimerRef = useRef<number | null>(null);
  const deleteCancelRef = useRef<HTMLButtonElement | null>(null);
  const requestedDocumentId = useRef<string | null>(null);

  useEffect(
    () => () => {
      if (copyResetTimerRef.current !== null) window.clearTimeout(copyResetTimerRef.current);
    },
    [],
  );

  useEffect(() => {
    if (!deleteTarget) return;
    deleteCancelRef.current?.focus();
    const closeOnEscape = (event: KeyboardEvent) => {
      if (event.key === "Escape") setDeleteTarget(null);
    };
    document.addEventListener("keydown", closeOnEscape);
    return () => document.removeEventListener("keydown", closeOnEscape);
  }, [deleteTarget]);

  useEffect(() => {
    if (token || resetToken) return;
    void apiRequest<{ enabled: boolean }>("/auth/google/config")
      .then((result) => setGoogleEnabled(result.enabled))
      .catch(() => setGoogleEnabled(false));
  }, [resetToken, token]);

  useEffect(() => {
    if (!invitationToken) return;
    void apiRequest<InvitationPreview>(
      `/team/invitations/preview?token=${encodeURIComponent(invitationToken)}`,
    )
      .then(setInvitationPreview)
      .catch((requestError: unknown) => setInvitationError(invitationErrorMessage(requestError)))
      .finally(() => setInvitationLoading(false));
  }, [invitationToken]);

  useEffect(() => {
    if (!invitationToken || !token || !user || !invitationPreview || invitationAccepted.current)
      return;
    invitationAccepted.current = true;
    void apiRequest(`/team/invitations/accept`, {
      method: "POST",
      body: JSON.stringify({ token: invitationToken }),
    })
      .then(() => {
        setStoredOrganizationId(invitationPreview.organization_id);
        setInvitationSuccess(`You're now a member of ${invitationPreview.organization_name}.`);
        window.history.replaceState({}, "", "/");
        setTimeout(() => window.location.assign("/"), 700);
      })
      .catch((requestError: unknown) => {
        invitationAccepted.current = false;
        setInvitationError(invitationErrorMessage(requestError));
      });
  }, [invitationPreview, invitationToken, token, user]);

  useEffect(() => {
    if (token || resetToken) return;
    if (googleAuthError) {
      const messages: Record<string, string> = {
        cancelled: "Google sign-in was cancelled.",
        invalid_state: "Google sign-in session expired. Please try again.",
        account_unavailable: "This account is not available for sign-in.",
        company_name_required: "Complete company setup before signing in with Google.",
        invitation_email_mismatch:
          "This invitation was sent to another email address. Retry with the invited Google account.",
        invitation_invalid:
          "This invitation is no longer valid. Ask the workspace administrator for a new invitation.",
        failed: "Unable to sign in with Google. Please try again.",
      };
      setError(messages[googleAuthError] ?? messages.failed);
      window.history.replaceState({}, "", window.location.pathname);
      return;
    }
    if (!googleLoginCode) return;
    setGoogleLoading(true);
    void apiRequest<{ access_token: string }>("/auth/google/exchange", {
      method: "POST",
      body: JSON.stringify({ code: googleLoginCode }),
    })
      .then((result) => {
        window.history.replaceState({}, "", window.location.pathname);
        setStoredToken(result.access_token);
        if (googleInvitation && googleInvitationOrganization) {
          setStoredOrganizationId(googleInvitationOrganization);
        }
        setToken(result.access_token);
      })
      .catch((requestError: unknown) => {
        window.history.replaceState({}, "", window.location.pathname);
        setError(
          requestError instanceof Error
            ? requestError.message
            : "Unable to sign in with Google. Please try again.",
        );
      })
      .finally(() => setGoogleLoading(false));
  }, [
    googleAuthError,
    googleInvitation,
    googleInvitationOrganization,
    googleLoginCode,
    resetToken,
    token,
  ]);

  useEffect(() => {
    if (token || resetToken) return;
    if (samlAuthError) {
      setError(
        samlAuthError === "unavailable"
          ? "Workspace SSO is unavailable. Check the workspace slug or contact its administrator."
          : "SAML sign-in could not be completed. Please try again or contact your workspace administrator.",
      );
      window.history.replaceState({}, "", window.location.pathname);
      return;
    }
    if (!samlLoginCode) return;
    setSamlLoading(true);
    void apiRequest<{ access_token: string; organization_id: string }>("/auth/saml/exchange", {
      method: "POST",
      body: JSON.stringify({ code: samlLoginCode }),
    })
      .then((result) => {
        window.history.replaceState({}, "", window.location.pathname);
        setStoredOrganizationId(result.organization_id);
        setStoredToken(result.access_token);
        setToken(result.access_token);
      })
      .catch(() => {
        window.history.replaceState({}, "", window.location.pathname);
        setError("SAML sign-in could not be completed. Please sign in again.");
      })
      .finally(() => setSamlLoading(false));
  }, [resetToken, samlAuthError, samlLoginCode, token]);

  useEffect(() => {
    return () => {
      if (previewUrl) URL.revokeObjectURL(previewUrl);
    };
  }, [previewUrl]);

  useEffect(() => {
    const resetExpiredSession = () => {
      setToken(null);
      setUser(null);
      setDocuments([]);
      setSelectedDocument(null);
      setPreviewUrl("");
      setSuccess("");
      setError("Your session expired. Please sign in again.");
    };
    window.addEventListener(AUTHENTICATION_LOST_EVENT, resetExpiredSession);
    return () => window.removeEventListener(AUTHENTICATION_LOST_EVENT, resetExpiredSession);
  }, []);

  async function loadCurrentUser() {
    try {
      const currentUser = await apiRequest<User>("/auth/me");
      if (
        currentUser.organization_id &&
        !localStorage.getItem("document_analyzer_organization_id")
      ) {
        setStoredOrganizationId(currentUser.organization_id);
        void loadDocuments();
      }
      setUser(currentUser);
      setPasswordChangeRequired(Boolean(currentUser.force_password_change));
    } catch {
      clearStoredToken();
      setToken(null);
      setUser(null);
    }
  }

  async function loadDocuments(showLoading = true) {
    if (showLoading) {
      setDocumentsLoading(true);
      setError("");
    }

    try {
      const params = new URLSearchParams({
        page: String(page),
        page_size: "20",
        sort_by: sortBy,
        sort_direction: sortDirection,
      });
      if (search) params.set("search", search);
      if (statusFilter) params.set("status", statusFilter);
      if (mimeFilter) params.set("mime_type", mimeFilter);
      const data = await apiRequest<PaginatedDocuments>(`/documents?${params.toString()}`);

      if (data.total_pages > 0 && page > data.total_pages) {
        setPage(data.total_pages);
        return;
      }
      setDocuments(data.items);
      setDocumentTotal(data.total);
      setTotalPages(data.total_pages);
    } catch (requestError) {
      if (requestError instanceof AuthenticationLostError) return;
      if (showLoading) {
        setError(
          requestError instanceof Error ? requestError.message : "Unable to load documents.",
        );
      }
    } finally {
      if (showLoading) {
        setDocumentsLoading(false);
      }
    }
  }

  useEffect(() => {
    if (!token) return;

    void loadCurrentUser();
  }, [token]);

  useEffect(() => {
    if (!token) return;
    void loadDocuments();
  }, [token, page, search, statusFilter, mimeFilter, sortBy, sortDirection]);

  useEffect(() => {
    const timeout = window.setTimeout(() => {
      setPage(1);
      setSearch(searchInput.trim());
    }, 350);
    return () => window.clearTimeout(timeout);
  }, [searchInput]);

  const hasActiveDocuments = documents.some(
    (document) =>
      document.status === "uploaded" ||
      document.status === "queued" ||
      document.status === "processing",
  );
  const canMutateDocuments = user?.role === "admin" || user?.role === "reviewer";

  useEffect(() => {
    if (!token || !hasActiveDocuments) return;

    const intervalId = window.setInterval(() => {
      void loadDocuments(false);
    }, DOCUMENT_POLL_INTERVAL_MS);

    return () => window.clearInterval(intervalId);
  }, [token, hasActiveDocuments]);

  useEffect(() => {
    if (!selectedDocument) return;

    const listedDocument = documents.find((document) => document.id === selectedDocument.id);
    if (listedDocument && listedDocument.status !== selectedDocument.status) {
      void openDocument(selectedDocument.id);
    }
  }, [documents, selectedDocument?.id, selectedDocument?.status]);

  async function handleAuth(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();

    setLoading(true);
    setError("");
    setSuccess("");

    try {
      if (authMode === "google_onboarding") {
        const onboarding = await apiRequest<{ google_code: string; organization_id: string }>(
          "/auth/google/complete-onboarding",
          {
            method: "POST",
            body: JSON.stringify({
              onboarding_token: googleOnboardingToken,
              company_name: companyName,
            }),
          },
        );
        setStoredOrganizationId(onboarding.organization_id);
        const data = await apiRequest<{ access_token: string }>("/auth/google/exchange", {
          method: "POST",
          body: JSON.stringify({ code: onboarding.google_code }),
        });
        window.history.replaceState({}, "", window.location.pathname);
        setStoredToken(data.access_token);
        setToken(data.access_token);
      } else if (authMode === "register") {
        const registration = await apiRequest<User & { organization_id: string }>(
          "/auth/register",
          {
            method: "POST",
            body: JSON.stringify({
              email,
              password,
              company_name: companyName,
            }),
          },
        );
        setStoredOrganizationId(registration.organization_id);

        setSuccess("Account created. Check your email to verify your address before signing in.");
        setVerificationPending(true);
        setAuthMode("login");
        setPassword("");
      } else {
        const data = await apiRequest<{ access_token: string; password_change_required?: boolean }>(
          "/auth/login",
          {
            method: "POST",
            body: JSON.stringify({
              email,
              password,
            }),
          },
        );

        setStoredToken(data.access_token);
        setToken(data.access_token);
        setPasswordChangeRequired(Boolean(data.password_change_required));
        setPassword("");
      }
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Authentication failed.");
    } finally {
      setLoading(false);
    }
  }

  function returnToLogin() {
    window.history.replaceState({}, "", window.location.pathname);
    setAuthMode("login");
    setPassword("");
    setConfirmPassword("");
    setError("");
    setSuccess("");
  }

  function startSamlSignIn(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const slug = samlOrganizationSlug.trim();
    if (!/^[a-zA-Z0-9](?:[a-zA-Z0-9-]{0,61}[a-zA-Z0-9])?$/.test(slug)) {
      setError("Enter the workspace slug provided by your administrator.");
      return;
    }
    setError("");
    const query = new URLSearchParams({ return_path: "/saml-login" });
    window.location.assign(
      `${API_BASE_URL}/auth/saml/${encodeURIComponent(slug)}/start?${query.toString()}`,
    );
  }

  async function handleForgotPassword(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setLoading(true);
    setError("");
    setSuccess("");
    try {
      const result = await apiRequest<{ detail: string }>("/auth/forgot-password", {
        method: "POST",
        body: JSON.stringify({ email }),
      });
      setSuccess(result.detail);
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Unable to request a reset.");
    } finally {
      setLoading(false);
    }
  }

  async function handleResetPassword(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setSuccess("");
    if (password !== confirmPassword) {
      setError("Passwords do not match.");
      return;
    }
    setLoading(true);
    try {
      const result = await apiRequest<{ detail: string }>("/auth/reset-password", {
        method: "POST",
        body: JSON.stringify({ token: resetToken, new_password: password }),
      });
      window.history.replaceState({}, "", window.location.pathname);
      setSuccess(result.detail);
      setPassword("");
      setConfirmPassword("");
    } catch (requestError) {
      setError(
        requestError instanceof Error
          ? requestError.message
          : "This password reset link is invalid or has expired.",
      );
    } finally {
      setLoading(false);
    }
  }

  async function handleRequiredPasswordChange(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setError("");
    setSuccess("");
    if (password !== confirmPassword) {
      setError("Passwords do not match.");
      return;
    }
    setLoading(true);
    try {
      const result = await apiRequest<{ detail: string }>("/auth/change-password", {
        method: "POST",
        body: JSON.stringify({ new_password: password }),
      });
      clearStoredToken();
      setToken(null);
      setUser(null);
      setPasswordChangeRequired(false);
      setPassword("");
      setConfirmPassword("");
      setSuccess(result.detail);
      setAuthMode("login");
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Unable to change password.");
    } finally {
      setLoading(false);
    }
  }

  function logout() {
    clearStoredToken();
    setToken(null);
    setUser(null);
    setPasswordChangeRequired(false);
    setDocuments([]);
    setSelectedDocumentIds(new Set());
    setSelectedDocument(null);
    setSuccess("");
    setError("");
  }

  async function handleUpload(event: React.ChangeEvent<HTMLInputElement>) {
    const file = event.target.files?.[0];

    if (!file) return;

    setUploading(true);
    setError("");
    setSuccess("");

    try {
      if (file.size > 10 * 1024 * 1024) {
        throw new Error("File exceeds the maximum allowed size of 10 MB.");
      }

      const formData = new FormData();
      formData.append("file", file);

      await apiRequest<DocumentItem>("/documents", {
        method: "POST",
        body: formData,
      });

      setSuccess("Document uploaded and queued for processing.");
      await loadDocuments();
    } catch (requestError) {
      setError(requestError instanceof Error ? requestError.message : "Upload failed.");
    } finally {
      setUploading(false);

      if (fileInputRef.current) {
        fileInputRef.current.value = "";
      }
    }
  }

  async function openDocument(documentId: string) {
    requestedDocumentId.current = documentId;
    setSelectedDocument((current) => (current?.id === documentId ? current : null));
    setPreviewUrl("");
    setDocumentAnalysis(null);
    setDocumentAnalysisError("");
    setError("");
    setDetailLoading(true);

    try {
      const data = await apiRequest<DocumentDetail>(`/documents/${documentId}`);

      if (requestedDocumentId.current !== documentId) return;
      setSelectedDocument(data);
      await loadPreview(documentId);
      await loadDocumentAnalysis(documentId);
    } catch (requestError) {
      if (requestError instanceof AuthenticationLostError) return;
      setError(requestError instanceof Error ? requestError.message : "Unable to open document.");
    } finally {
      setDetailLoading(false);
    }
  }

  async function loadDocumentAnalysis(documentId: string) {
    setDocumentAnalysisLoading(true);
    setDocumentAnalysisError("");
    try {
      const data = await apiRequest<DocumentAnalysis>(`/documents/${documentId}/ai-analysis`);
      if (requestedDocumentId.current === documentId) setDocumentAnalysis(data);
    } catch (requestError) {
      if (requestError instanceof AuthenticationLostError) return;
      setDocumentAnalysisError(
        requestError instanceof Error ? requestError.message : "Unable to load analysis.",
      );
    } finally {
      setDocumentAnalysisLoading(false);
    }
  }

  async function loadPreview(documentId: string) {
    setPreviewLoading(true);
    setPreviewError("");
    try {
      const response = await authenticatedFetch(`/documents/${documentId}/file`);
      if (!response.ok) throw new Error("Preview is unavailable for this document.");
      const nextUrl = URL.createObjectURL(await response.blob());
      if (requestedDocumentId.current === documentId) setPreviewUrl(nextUrl);
      else URL.revokeObjectURL(nextUrl);
    } catch (requestError) {
      if (requestError instanceof AuthenticationLostError) return;
      setPreviewUrl("");
      setPreviewError(requestError instanceof Error ? requestError.message : "Preview failed.");
    } finally {
      setPreviewLoading(false);
    }
  }

  async function deleteDocument(documentId: string) {
    setError("");
    setSuccess("");

    try {
      await apiRequest<void>(`/documents/${documentId}`, {
        method: "DELETE",
      });

      if (selectedDocument?.id === documentId) {
        setSelectedDocument(null);
      }
      setSelectedDocumentIds((current) => {
        const next = new Set(current);
        next.delete(documentId);
        return next;
      });

      setSuccess("Document deleted.");
      await loadDocuments();
    } catch (requestError) {
      if (requestError instanceof AuthenticationLostError) return;
      setError(requestError instanceof Error ? requestError.message : "Unable to delete document.");
    }
  }

  function containDeleteDialogFocus(event: React.KeyboardEvent<HTMLDivElement>) {
    if (event.key !== "Tab") return;
    const controls = Array.from(
      event.currentTarget.querySelectorAll<HTMLButtonElement>("button:not(:disabled)"),
    );
    if (controls.length === 0) return;
    const first = controls[0];
    const last = controls[controls.length - 1];
    if (event.shiftKey && document.activeElement === first) {
      event.preventDefault();
      last.focus();
    } else if (!event.shiftKey && document.activeElement === last) {
      event.preventDefault();
      first.focus();
    }
  }

  async function reprocessDocument(documentId: string) {
    setError("");
    setSuccess("");

    try {
      await apiRequest<DocumentDetail>(`/documents/${documentId}/reprocess`, {
        method: "POST",
      });

      setSuccess("Document has been queued for reprocessing.");
      await loadDocuments();

      if (selectedDocument?.id === documentId) {
        await openDocument(documentId);
      }
    } catch (requestError) {
      if (requestError instanceof AuthenticationLostError) return;
      setError(
        requestError instanceof Error ? requestError.message : "Unable to reprocess document.",
      );
    }
  }

  async function downloadExport(documentId: string, type: "csv" | "xlsx" | "json") {
    setError("");
    setExporting(type);

    try {
      const response = await authenticatedFetch(`/documents/${documentId}/exports/${type}`);

      if (!response.ok) {
        throw new Error(`Unable to export ${type.toUpperCase()} file.`);
      }

      const blob = await response.blob();
      const url = URL.createObjectURL(blob);

      const link = document.createElement("a");
      link.href = url;
      const disposition = response.headers.get("content-disposition") ?? "";
      link.download = disposition.match(/filename="?([^";]+)"?/i)?.[1] ?? `invoice-export.${type}`;
      document.body.appendChild(link);
      link.click();
      link.remove();

      URL.revokeObjectURL(url);
    } catch (requestError) {
      if (requestError instanceof AuthenticationLostError) return;
      setError(requestError instanceof Error ? requestError.message : "Export failed.");
    } finally {
      setExporting(null);
    }
  }

  async function downloadBulkExport(type: "csv" | "xlsx" | "json") {
    if (selectedDocumentIds.size === 0) return;
    setExporting(type);
    setError("");
    try {
      const response = await authenticatedFetch(`/exports/${type}`, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: JSON.stringify({ document_ids: [...selectedDocumentIds] }),
      });
      if (!response.ok) throw new Error("Unable to export the selected documents.");
      const blob = await response.blob();
      const url = URL.createObjectURL(blob);
      const link = document.createElement("a");
      link.href = url;
      const disposition = response.headers.get("content-disposition") ?? "";
      link.download = disposition.match(/filename="?([^";]+)"?/i)?.[1] ?? `invoice-export.${type}`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (requestError) {
      if (requestError instanceof AuthenticationLostError) return;
      setError(requestError instanceof Error ? requestError.message : "Export failed.");
    } finally {
      setExporting(null);
    }
  }

  async function loadAnalytics() {
    if (analyticsRange === "custom" && (!customStartDate || !customEndDate)) {
      setAnalytics(null);
      setInsightsLoading(false);
      setInsightsError("");
      return;
    }
    setInsightsLoading(true);
    setInsightsError("");
    try {
      const data = await apiRequest<Analytics>(`/analytics/summary?${insightsQuery()}`);
      if (!data?.kpis || !data?.trends) throw new Error("Analytics response is incomplete.");
      setAnalytics(data);
    } catch (requestError) {
      if (requestError instanceof AuthenticationLostError) return;
      setInsightsError(
        requestError instanceof Error ? requestError.message : "Unable to load analytics.",
      );
    } finally {
      setInsightsLoading(false);
    }
  }

  function insightsQuery() {
    const params = new URLSearchParams({ range: analyticsRange });
    if (analyticsRange === "custom") {
      if (customStartDate) params.set("start_date", customStartDate);
      if (customEndDate) params.set("end_date", customEndDate);
    }
    return params.toString();
  }

  async function loadFindings() {
    setInsightsLoading(true);
    setInsightsError("");
    try {
      const params = new URLSearchParams({ page_size: "50" });
      if (findingSeverity) params.set("severity", findingSeverity);
      if (findingStatus) params.set("status", findingStatus);
      const data = await apiRequest<{ items: AIFinding[] }>(
        `/ai-analysis/findings?${params.toString()}`,
      );
      if (!Array.isArray(data?.items)) throw new Error("AI findings response is incomplete.");
      setFindings(data.items);
      setSelectedFinding((current) =>
        current ? (data.items.find((finding) => finding.id === current.id) ?? null) : null,
      );
    } catch (requestError) {
      if (requestError instanceof AuthenticationLostError) return;
      setInsightsError(
        requestError instanceof Error ? requestError.message : "Unable to load AI findings.",
      );
    } finally {
      setInsightsLoading(false);
    }
  }

  useEffect(() => {
    if (!token || !user || workspaceView === "documents") return;
    if (workspaceView === "overview") void loadAnalytics();
    if (workspaceView === "ai-analysis") void loadFindings();
  }, [
    token,
    user,
    workspaceView,
    analyticsRange,
    customStartDate,
    customEndDate,
    findingSeverity,
    findingStatus,
  ]);

  async function loadTeam() {
    setTeamLoading(true);
    setTeamError("");
    try {
      const [members, invitations] = await Promise.all([
        apiRequest<TeamMember[]>("/team/members"),
        apiRequest<TeamInvitation[]>("/team/invitations"),
      ]);
      setTeamMembers(members);
      setTeamInvitations(invitations);
    } catch (requestError) {
      setTeamError(requestError instanceof Error ? requestError.message : "Unable to load team.");
    } finally {
      setTeamLoading(false);
    }
  }

  async function loadUsage() {
    setUsageLoading(true);
    setUsageError("");
    try {
      const data: unknown = await apiRequest<UsageSnapshot>("/organizations/usage");
      if (!isUsageSnapshot(data)) throw new Error("Usage information is unavailable right now.");
      setUsage(data);
    } catch (requestError) {
      if (requestError instanceof AuthenticationLostError) return;
      setUsageError(
        requestError instanceof Error
          ? requestError.message
          : "Usage information could not be loaded.",
      );
    } finally {
      setUsageLoading(false);
    }
  }

  useEffect(() => {
    if (token && user && workspaceView === "team") void loadTeam();
  }, [token, user, workspaceView]);

  useEffect(() => {
    if (token && user && workspaceView === "usage") void loadUsage();
  }, [token, user, user?.organization_id, workspaceView]);

  async function inviteMember(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setTeamLoading(true);
    setTeamError("");
    setTeamSuccess("");
    try {
      const result = await apiRequest<TeamInvitation>("/team/invitations", {
        method: "POST",
        body: JSON.stringify({ email: inviteEmail, role: inviteRole }),
      });
      setInviteOpen(false);
      setInviteEmail("");
      if (result.invitation_token) {
        setInvitationLink({
          id: result.id,
          email: result.invited_email,
          url: `${PUBLIC_APP_ORIGIN}/invite/accept?token=${encodeURIComponent(result.invitation_token)}`,
        });
      }
      setTeamSuccess(`Invitation sent to ${result.invited_email}.`);
      await loadTeam();
    } catch (requestError) {
      setTeamError(teamErrorMessage(requestError));
    } finally {
      setTeamLoading(false);
    }
  }

  const currentMembership = teamMembers.find((member) => member.user_id === user?.id);
  const currentRole = currentMembership?.role ?? user?.membership_role;
  const canManageTeam = currentRole === "owner" || currentRole === "admin";
  const canInviteAdmin = currentRole === "owner";
  const companySearch = teamSearch.trim().toLocaleLowerCase();
  const filteredTeamMembers = teamMembers.filter((member) =>
    member.email.toLocaleLowerCase().includes(companySearch),
  );
  const filteredTeamInvitations = teamInvitations.filter((invitation) =>
    invitation.invited_email.toLocaleLowerCase().includes(companySearch),
  );

  async function runTeamAction() {
    if (!teamAction) return;
    setTeamActionLoading(true);
    setTeamError("");
    setTeamSuccess("");
    try {
      if (teamAction.kind === "role") {
        await apiRequest(`/team/members/${teamAction.member.id}`, {
          method: "PATCH",
          body: JSON.stringify({ role: roleUpdate }),
        });
        setTeamSuccess("Role updated.");
      } else if (teamAction.kind === "deactivate") {
        await apiRequest(`/team/members/${teamAction.member.id}`, { method: "DELETE" });
        setTeamSuccess(
          "Workspace access deactivated. The user account and workspace data remain intact.",
        );
      } else if (teamAction.kind === "reset") {
        const result = await apiRequest<{ temporary_password?: string }>(
          `/team/members/${teamAction.member.id}/password-reset`,
          {
            method: "POST",
          },
        );
        setTeamSuccess(
          result.temporary_password
            ? `Temporary password (shown once): ${result.temporary_password}`
            : "Password reset initiated. The current password remains private.",
        );
      } else {
        await apiRequest(`/team/invitations/${teamAction.invitation.id}/revoke`, {
          method: "POST",
        });
        setTeamSuccess("Invitation cancelled. Its existing link will no longer work.");
      }
      setTeamAction(null);
      await loadTeam();
    } catch (requestError) {
      setTeamError(teamErrorMessage(requestError));
    } finally {
      setTeamActionLoading(false);
    }
  }

  async function resendInvitation(invitation: TeamInvitation) {
    setTeamLoading(true);
    setTeamError("");
    try {
      const result = await apiRequest<TeamInvitation>(`/team/invitations/${invitation.id}/resend`, {
        method: "POST",
      });
      if (result.invitation_token) {
        setInvitationLink({
          id: result.id,
          email: result.invited_email,
          url: `${PUBLIC_APP_ORIGIN}/invite/accept?token=${encodeURIComponent(result.invitation_token)}`,
        });
      }
      setNewLinkInvitationId(invitation.id);
      setTeamSuccess(
        `New invitation link created for ${invitation.invited_email}. The previous link no longer works.`,
      );
      await loadTeam();
    } catch (requestError) {
      setTeamError(teamErrorMessage(requestError));
    } finally {
      setTeamLoading(false);
    }
  }

  async function copyInvitationLink(invitationId: string, url: string) {
    try {
      await navigator.clipboard.writeText(url);
      if (copyResetTimerRef.current !== null) window.clearTimeout(copyResetTimerRef.current);
      setCopiedInvitationId(invitationId);
      copyResetTimerRef.current = window.setTimeout(() => {
        setCopiedInvitationId(null);
        copyResetTimerRef.current = null;
      }, 2000);
      setTeamSuccess("Invitation link copied to your clipboard.");
    } catch {
      setTeamError("We couldn't copy the invitation link. Select and copy it manually.");
    }
  }

  async function updateFindingStatus(
    finding: AIFinding,
    status: "open" | "acknowledged" | "resolved",
  ) {
    setInsightsError("");
    try {
      await apiRequest(`/ai-analysis/findings/${finding.id}`, {
        method: "PATCH",
        body: JSON.stringify({ status }),
      });
      await loadFindings();
      setSuccess(`AI finding marked ${status}.`);
    } catch (requestError) {
      setInsightsError(
        requestError instanceof Error ? requestError.message : "Unable to update AI finding.",
      );
    }
  }

  async function generateReport() {
    setReportLoading(true);
    setInsightsError("");
    try {
      const data = await apiRequest<Record<string, unknown>>(
        `/reports/${reportType}?${insightsQuery()}`,
      );
      setReport(data);
    } catch (requestError) {
      if (requestError instanceof AuthenticationLostError) return;
      setReport(null);
      setInsightsError(
        requestError instanceof Error ? requestError.message : "Unable to generate report.",
      );
    } finally {
      setReportLoading(false);
    }
  }

  async function downloadReport(format: "csv" | "xlsx" | "json") {
    setExporting(format);
    setInsightsError("");
    try {
      const response = await authenticatedFetch(
        `/reports/${reportType}/exports/${format}?${insightsQuery()}`,
      );
      if (!response.ok) throw new Error("Unable to export report.");
      const url = URL.createObjectURL(await response.blob());
      const link = document.createElement("a");
      link.href = url;
      const disposition = response.headers.get("content-disposition") ?? "";
      link.download = disposition.match(/filename="?([^";]+)"?/i)?.[1] ?? `invoxa-report.${format}`;
      document.body.appendChild(link);
      link.click();
      link.remove();
      URL.revokeObjectURL(url);
    } catch (requestError) {
      setInsightsError(
        requestError instanceof Error ? requestError.message : "Unable to export report.",
      );
    } finally {
      setExporting(null);
    }
  }

  async function acceptNewInvitation(event: React.FormEvent<HTMLFormElement>) {
    event.preventDefault();
    setInvitationLoading(true);
    setInvitationError("");
    try {
      const result = await apiRequest<{ access_token: string; organization_id: string }>(
        "/team/invitations/accept-new",
        {
          method: "POST",
          body: JSON.stringify({
            token: invitationToken,
            email: invitationPreview?.invited_email,
            password: invitationPassword,
          }),
        },
      );
      setStoredToken(result.access_token);
      setStoredOrganizationId(result.organization_id);
      setInvitationSuccess(`You're now a member of ${invitationPreview?.organization_name}.`);
      window.history.replaceState({}, "", "/");
      setToken(result.access_token);
    } catch (requestError) {
      setInvitationError(invitationErrorMessage(requestError));
    } finally {
      setInvitationLoading(false);
    }
  }

  if (pricingRoute) return <PricingPage />;

  if (inviteRoute && !token && invitationMode !== "login") {
    return (
      <main className="auth-page invitation-auth-page">
        <section className="auth-card invitation-card" aria-labelledby="invite-accept-heading">
          <img className="auth-logo" src="/invoxa-logo.jpg" alt="Invoxa" />
          <h1 id="invite-accept-heading">You're invited to join Invoxa</h1>
          <p className="auth-intro">
            Upload and process business documents, review extracted information with AI, and export
            structured invoice data.
          </p>

          {(authMode === "login" || authMode === "register") && (
            <a className="auth-plans-link" href="/pricing">Explore plans and pricing</a>
          )}
          {invitationLoading && <p role="status">Checking your invitation...</p>}
          {invitationError && (
            <div className="alert error" role="alert">
              {invitationError}
            </div>
          )}
          {invitationSuccess && (
            <div className="alert success" role="status">
              {invitationSuccess}
            </div>
          )}
          {invitationPreview && !invitationSuccess && (
            <>
              <dl className="invitation-context">
                <div>
                  <dt>Workspace</dt>
                  <dd>{invitationPreview.organization_name}</dd>
                </div>
                <div>
                  <dt>Your role</dt>
                  <dd>{teamRoleLabel(invitationPreview.role)}</dd>
                </div>
              </dl>
              <p className="invite-copy">
                Join this workspace to work with its documents and day-to-day invoice operations.
              </p>
              {invitationMode === "choose" ? (
                <div className="invite-actions">
                  <button
                    className="primary-button invitation-primary"
                    type="button"
                    onClick={() => setInvitationMode("login")}
                  >
                    Sign in to accept
                  </button>
                  <div className="auth-divider" aria-hidden="true">
                    OR
                  </div>
                  <button
                    className="secondary-button"
                    type="button"
                    onClick={() => setInvitationMode("new")}
                  >
                    Create account and join
                  </button>
                </div>
              ) : (
                <form onSubmit={acceptNewInvitation} className="auth-form invite-form">
                  <label>
                    Email
                    <input type="email" value={invitationPreview.invited_email} readOnly />
                  </label>
                  <label>
                    Password
                    <input
                      type="password"
                      minLength={12}
                      required
                      value={invitationPassword}
                      onChange={(event) => setInvitationPassword(event.target.value)}
                      autoComplete="new-password"
                    />
                  </label>
                  <p className="dialog-help">
                    Use at least 12 characters. You are joining this workspace; no new company is
                    created.
                  </p>
                  <button
                    className="primary-button invitation-primary"
                    disabled={invitationLoading}
                  >
                    {invitationLoading ? "Joining..." : "Create account and join"}
                  </button>
                </form>
              )}
            </>
          )}
          {!invitationPreview && !invitationLoading && !invitationError && (
            <p role="alert">This invitation is no longer valid.</p>
          )}
        </section>
      </main>
    );
  }

  if (token && passwordChangeRequired) {
    return (
      <main className="auth-page">
        <section className="auth-card">
          <img
            className="auth-logo"
            src="/invoxa-logo.jpg"
            alt="Invoxa — Document & Invoice Analyzer"
          />
          <h1>Choose your private password</h1>
          <p className="auth-intro">
            Your manager provided a temporary password. Choose a private password before using
            Invoxa.
          </p>
          <form onSubmit={handleRequiredPasswordChange} className="auth-form">
            <label>
              New password
              <input
                type="password"
                value={password}
                onChange={(event) => setPassword(event.target.value)}
                autoComplete="new-password"
                minLength={12}
                required
              />
            </label>
            <label>
              Confirm password
              <input
                type="password"
                value={confirmPassword}
                onChange={(event) => setConfirmPassword(event.target.value)}
                autoComplete="new-password"
                minLength={12}
                required
              />
            </label>
            {error && (
              <div className="alert error" role="alert">
                {error}
              </div>
            )}
            <button className="primary-button" disabled={loading}>
              {loading ? "Changing..." : "Change password"}
            </button>
          </form>
        </section>
      </main>
    );
  }

  if (!token || !user) {
    if (verificationPending) {
      return (
        <main className="auth-page">
          <section className="auth-card" aria-labelledby="verification-heading">
            <img className="auth-logo" src="/invoxa-logo.jpg" alt="Invoxa" />
            <h1 id="verification-heading">Verify your email address</h1>
            <p>Confirm your email to securely access your workspace.</p>
            <form className="auth-form" onSubmit={async (event) => {
              event.preventDefault(); setLoading(true); setError("");
              try {
                if (verificationToken) {
                  await apiRequest("/auth/verify-email", {
                    method: "POST", body: JSON.stringify({ token: verificationToken }),
                  });
                  setVerificationToken(""); setVerificationPending(false);
                  setAuthMode("login"); setSuccess("Email verified. You can now sign in.");
                } else {
                  await apiRequest("/auth/resend-verification", {
                    method: "POST", body: JSON.stringify({ email }),
                  });
                  setSuccess("If verification is needed, a verification email will be sent.");
                }
              } catch (requestError) {
                setError(requestError instanceof Error ? requestError.message : "Verification unavailable.");
                setVerificationToken("");
              } finally { setLoading(false); }
            }}>
              {!verificationToken && <label>Email<input type="email" value={email}
                onChange={(event) => setEmail(event.target.value)} required autoComplete="email" /></label>}
              {error && <div className="alert error" role="alert">{error}</div>}
              {success && <div className="alert success" role="status">{success}</div>}
              <button className="primary-button" disabled={loading}>
                {loading ? "Please wait..." : verificationToken ? "Verify email" : "Resend verification email"}
              </button>
              <button className="text-button" type="button" onClick={() => {
                setVerificationPending(false); setAuthMode("login");
              }}>Back to sign in</button>
            </form>
          </section>
        </main>
      );
    }
    return (
      <main className="auth-page">
        <section className="auth-card">
          <img
            className="auth-logo"
            src="/invoxa-logo.jpg"
            alt="Invoxa — Document & Invoice Analyzer"
          />

          <h1 className={authMode === "login" ? "auth-login-heading" : undefined}>
            {authMode === "login"
              ? "Sign into Invoxa"
              : authMode === "register"
                ? "Create your Invoxa account"
                : authMode === "forgot"
                  ? "Forgot your password?"
                  : authMode === "reset"
                    ? "Reset your password"
                    : "Set up your Invoxa workspace"}
          </h1>

          <p className="auth-intro">
            {authMode === "login" || authMode === "register"
              ? "Upload and process business documents, review extracted information with AI, and export structured invoice data."
              : authMode === "forgot"
                ? "Enter your account email and we’ll send password reset instructions."
                : authMode === "reset"
                  ? "Choose a new password with at least 12 characters."
                  : "Choose a company or workspace name to finish setting up your verified Google account."}
          </p>

          {(googleLoading || samlLoading) && (
            <div className="alert" role="status">
              Completing sign-in…
            </div>
          )}

          {authMode === "forgot" ? (
            <form onSubmit={handleForgotPassword} className="auth-form">
              <label>
                Email
                <input
                  type="email"
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  autoComplete="email"
                  required
                />
              </label>
              {error && <div className="alert error">{error}</div>}
              {success && (
                <div className="alert success" role="status">
                  {success}
                </div>
              )}
              <button className="primary-button" disabled={loading}>
                {loading ? "Sending..." : "Reset Password"}
              </button>
              <button className="text-button" type="button" onClick={returnToLogin}>
                Back to sign in
              </button>
            </form>
          ) : authMode === "reset" ? (
            <form onSubmit={handleResetPassword} className="auth-form">
              <label>
                New password
                <input
                  type={showPassword ? "text" : "password"}
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  autoComplete="new-password"
                  minLength={12}
                  required
                />
              </label>
              <label>
                Confirm password
                <input
                  type={showPassword ? "text" : "password"}
                  value={confirmPassword}
                  onChange={(event) => setConfirmPassword(event.target.value)}
                  autoComplete="new-password"
                  minLength={12}
                  required
                />
              </label>
              {error && <div className="alert error">{error}</div>}
              {success && (
                <div className="alert success" role="status">
                  {success}
                </div>
              )}
              <button className="primary-button" disabled={loading}>
                {loading ? "Resetting..." : "Reset password"}
              </button>
              <button className="text-button" type="button" onClick={returnToLogin}>
                Back to sign in
              </button>
            </form>
          ) : authMode === "google_onboarding" ? (
            <form onSubmit={handleAuth} className="auth-form">
              <label htmlFor="google-company-name">
                Company or workspace name
                <span className="auth-input-wrap">
                  <input
                    id="google-company-name"
                    type="text"
                    value={companyName}
                    onChange={(event) => setCompanyName(event.target.value)}
                    placeholder="Acme Logistics"
                    autoComplete="organization"
                    maxLength={255}
                    required
                  />
                </span>
              </label>
              {error && (
                <div className="alert error" role="alert">
                  {error}
                </div>
              )}
              <button className="primary-button" disabled={loading}>
                {loading ? "Setting up workspace..." : "Continue to Invoxa"}
              </button>
            </form>
          ) : (
            <>
              <form onSubmit={handleAuth} className="auth-form">
                {authMode === "register" && (
                  <label htmlFor="auth-company-name">
                    Company or workspace name
                    <span className="auth-input-wrap">
                      <input
                        id="auth-company-name"
                        type="text"
                        value={companyName}
                        onChange={(event) => setCompanyName(event.target.value)}
                        placeholder="Acme Logistics"
                        autoComplete="organization"
                        maxLength={255}
                        required
                      />
                    </span>
                  </label>
                )}
                <label htmlFor="auth-email">
                  Email
                  <span className="auth-input-wrap">
                    <input
                      id="auth-email"
                      type="email"
                      value={email}
                      onChange={(event) => setEmail(event.target.value)}
                      placeholder="you@example.com"
                      autoComplete="email"
                      required
                    />
                  </span>
                </label>

                <label htmlFor="auth-password">
                  Password
                  <span className="auth-input-wrap">
                    <input
                      id="auth-password"
                      aria-label="Password"
                      type={showPassword ? "text" : "password"}
                      value={password}
                      onChange={(event) => setPassword(event.target.value)}
                      placeholder={
                        authMode === "register" ? "Minimum 12 characters" : "Your password"
                      }
                      autoComplete={authMode === "register" ? "new-password" : "current-password"}
                      minLength={authMode === "register" ? 12 : 1}
                      required
                    />
                    <button
                      className="password-toggle"
                      type="button"
                      aria-label={showPassword ? "Hide password" : "Show password"}
                      aria-pressed={showPassword}
                      onClick={() => setShowPassword((visible) => !visible)}
                    >
                      {showPassword ? "Hide" : "Show"}
                    </button>
                  </span>
                </label>

                {authMode === "login" && (
                  <div className="auth-options">
                    <button
                      className="text-button"
                      type="button"
                      onClick={() => {
                        setAuthMode("forgot");
                        setError("");
                        setSuccess("");
                      }}
                    >
                      Forgot password?
                    </button>
                  </div>
                )}

                {error && (
                  <div className="alert error" role="alert">
                    {error}
                  </div>
                )}
                {success && (
                  <div className="alert success" role="status">
                    {success}
                  </div>
                )}

                <button className="primary-button" disabled={loading}>
                  {loading ? "Please wait..." : authMode === "login" ? "Sign in" : "Create account"}
                </button>
              </form>
              {authMode === "login" && (
                <div className="auth-external-options">
                  <button
                    className="text-button"
                    type="button"
                    onClick={() => {
                      setVerificationPending(true);
                      setVerificationToken("");
                      setError("");
                      setSuccess("");
                    }}
                  >
                    Verify email or resend verification
                  </button>
                  {samlLoading && (
                    <div className="alert" role="status">
                      Completing workspace sign-in...
                    </div>
                  )}
                  <div className="auth-divider">
                    <span>or</span>
                  </div>
                  {googleEnabled ? (
                    <a className="google-button" href={`${API_BASE_URL}/auth/google/start`}>
                      <GoogleMark />
                      Continue with Google
                    </a>
                  ) : (
                    <button
                      className="google-button"
                      type="button"
                      disabled
                      title="Google Sign-In requires administrator configuration"
                    >
                      <GoogleMark />
                      Continue with Google
                    </button>
                  )}
                  <button
                    className="google-button sso-button"
                    type="button"
                    onClick={() => {
                      setSamlEntryOpen((open) => !open);
                      setError("");
                    }}
                  >
                    Continue with workspace SSO
                  </button>
                  {samlEntryOpen && (
                    <form className="sso-entry-form" onSubmit={startSamlSignIn}>
                      <label>
                        Workspace slug
                        <input
                          value={samlOrganizationSlug}
                          onChange={(event) => setSamlOrganizationSlug(event.target.value)}
                          autoComplete="organization"
                          maxLength={63}
                          placeholder="Provided by your administrator"
                          required
                        />
                      </label>
                      <button className="primary-button" type="submit">
                        Continue to SSO
                      </button>
                    </form>
                  )}
                </div>
              )}
              <p className="auth-switch">
                {authMode === "login" ? "Don't have an account?" : "Already have an account?"}{" "}
                <button
                  className="text-button"
                  type="button"
                  onClick={() => {
                    setAuthMode(authMode === "login" ? "register" : "login");
                    setError("");
                    setSuccess("");
                    setPassword("");
                  }}
                >
                  {authMode === "login" ? "Create now" : "Sign in"}
                </button>
              </p>
            </>
          )}

          <p className="security-note">Secure. Private. Built for your business.</p>
        </section>
      </main>
    );
  }

  return (
    <main className="app-shell">
      <button
        className={`sidebar-scrim ${sidebarOpen ? "visible" : ""}`}
        aria-label="Close navigation"
        onClick={() => setSidebarOpen(false)}
        type="button"
      />
      <aside className={`sidebar ${sidebarOpen ? "open" : ""}`}>
        <div className="sidebar-brand">
          <img src="/invoxa-logo.jpg" alt="Invoxa — Document & Invoice Analyzer" />
        </div>
        <nav className="sidebar-nav" aria-label="Primary workspace">
          {(
            ["overview", "ai-analysis", "reports", "documents", "company", "team", "usage"] as WorkspaceView[]
          ).map((view) => (
            <button
              className={workspaceView === view ? "active" : ""}
              key={view}
              onClick={() => {
                setWorkspaceView(view);
                setInsightsError("");
                setSidebarOpen(false);
              }}
              type="button"
              aria-current={workspaceView === view ? "page" : undefined}
            >
              <NavigationIcon view={view} />
              <span>{workspaceLabels[view].title}</span>
            </button>
          ))}
        </nav>
        <div className="sidebar-account">
          <div className="account-summary">
            <span className="account-avatar" aria-hidden="true">
              {user.email.charAt(0).toUpperCase()}
            </span>
            <div>
              <strong>{user.email}</strong>
              <small>{user.organization_name ?? "Workspace"}</small>
            </div>
          </div>
          <a className="sidebar-plans-link" href="/pricing">Plans & pricing</a>
          <button className="logout-button" type="button" onClick={() => { setWorkspaceView("account"); setSidebarOpen(false); }}>Account settings</button>
          <button className="logout-button" onClick={logout} type="button">
            <svg
              aria-hidden="true"
              className="nav-icon"
              fill="none"
              viewBox="0 0 24 24"
              stroke="currentColor"
              strokeWidth="1.8"
            >
              <path d="M10 5H5v14h5M14 8l4 4-4 4M8 12h10" />
            </svg>
            Log out
          </button>
        </div>
      </aside>

      <div className="app-main">
        <header className="topbar">
          <button
            className="mobile-menu-button"
            onClick={() => setSidebarOpen(true)}
            aria-label="Open navigation"
            type="button"
          >
            <span />
            <span />
            <span />
          </button>
          <div className="mobile-brand">
            <img src="/invoxa-logo.jpg" alt="Invoxa" />
          </div>
          <div className="topbar-user">
            <span className="account-avatar" aria-hidden="true">
              {user.email.charAt(0).toUpperCase()}
            </span>
            <span>{user.email}</span>
          </div>
        </header>

        <section className="dashboard">
          <div className="dashboard-heading">
            <div>
              <p className="eyebrow">Invoxa workspace</p>
              <h1
                id={workspaceView === "team" ? "team-heading" : undefined}
                className={workspaceView === "team" ? "team-page-title" : undefined}
              >
                {workspaceLabels[workspaceView].title}
              </h1>
              <p>{workspaceLabels[workspaceView].description}</p>
            </div>

            {workspaceView === "documents" && canMutateDocuments && (
              <label className="upload-button">
                <span aria-hidden="true">↑</span>
                {uploading ? "Uploading..." : "Upload document"}
                <input
                  ref={fileInputRef}
                  type="file"
                  accept=".pdf,.bmp,.gif,.jpg,.jpeg,.png,.tif,.tiff,.webp"
                  onChange={handleUpload}
                  disabled={uploading}
                />
              </label>
            )}
          </div>

          {error && <div className="alert error">{error}</div>}
          {success && <div className="alert success">{success}</div>}

          {workspaceView === "account" && <CustomerAccount />}

          {workspaceView === "company" && (
            <OrganizationSettings canEdit={canManageTeam} />
          )}

          {workspaceView === "team" && (
            <section className="team-page" aria-labelledby="team-heading">
              <div className="team-toolbar">
                <label className="team-search-label" htmlFor="team-search">
                  <span>Search members and invitations</span>
                  <input
                    id="team-search"
                    type="search"
                    value={teamSearch}
                    onChange={(event) => setTeamSearch(event.target.value)}
                    placeholder="Name or email"
                  />
                </label>
                {canManageTeam && (
                  <button
                    className="primary-button"
                    type="button"
                    onClick={() => setInviteOpen(true)}
                  >
                    Invite member
                  </button>
                )}
              </div>
              {teamError && (
                <div className="alert error team-alert" role="alert">
                  <TeamAlertIcon />
                  <span>{teamError}</span>
                </div>
              )}
              {teamSuccess && (
                <div className="alert success team-alert" role="status">
                  <TeamSuccessIcon />
                  <span>{teamSuccess}</span>
                </div>
              )}
              {invitationLink && (
                <div className="invitation-link-panel" role="status">
                  <div>
                    <strong>Invitation link for {invitationLink.email}</strong>
                    <p>Share this link with the invitee. It is only valid for this invitation.</p>
                  </div>
                  <div className="invitation-link-controls">
                    <input
                      aria-label={`Invitation link for ${invitationLink.email}`}
                      value={invitationLink.url}
                      readOnly
                      onFocus={(event) => event.currentTarget.select()}
                    />
                    <button
                      className="icon-button"
                      type="button"
                      aria-label={
                        copiedInvitationId === invitationLink.id ? "Copied" : "Copy invitation link"
                      }
                      title={
                        copiedInvitationId === invitationLink.id ? "Copied" : "Copy invitation link"
                      }
                      onClick={() => void copyInvitationLink(invitationLink.id, invitationLink.url)}
                    >
                      {copiedInvitationId === invitationLink.id ? <CheckIcon /> : <CopyIcon />}
                    </button>
                  </div>
                </div>
              )}
              {teamLoading ? (
                <div className="compact-state">Loading team access...</div>
              ) : (
                <>
                  <div className="team-summary">
                    <div>
                      <strong>{teamMembers.length}</strong>
                      <span>Active members</span>
                    </div>
                    <div>
                      <strong>{teamInvitations.length}</strong>
                      <span>Pending invitations</span>
                    </div>
                  </div>
                  <div className="team-card">
                    <h3>Members</h3>
                    {teamMembers.length === 0 ? (
                      <div className="compact-state">No active members.</div>
                    ) : filteredTeamMembers.length === 0 ? (
                      <div className="compact-state">No members match this search.</div>
                    ) : (
                      <div className="team-table-wrap">
                        <table className="team-table">
                          <colgroup>
                            <col className="team-col-member" />
                            <col className="team-col-role" />
                            <col className="team-col-status" />
                            <col className="team-col-date" />
                            <col className="team-col-actions" />
                          </colgroup>
                          <thead>
                            <tr>
                              <th>Member</th>
                              <th>Role</th>
                              <th>Status</th>
                              <th>Joined</th>
                              <th aria-label="Actions">{canManageTeam ? "Actions" : null}</th>
                            </tr>
                          </thead>
                          <tbody>
                          {filteredTeamMembers.map((member) => (
                              <tr key={member.id}>
                                <td>{member.email}</td>
                                <td>{teamRoleLabel(member.role)}</td>
                                <td>{member.status}</td>
                                <td>
                                  {member.joined_at
                                    ? new Date(member.joined_at).toLocaleDateString()
                                    : "—"}
                                </td>
                                <td>
                                  {canManageTeam ? (
                                    member.user_id !== user?.id &&
                                    ((currentMembership?.role === "owner" &&
                                      member.role !== "owner") ||
                                      (currentMembership?.role === "admin" &&
                                        member.role === "member")) ? (
                                      <details className="team-actions">
                                        <summary aria-label={`Actions for ${member.email}`}>
                                          Actions
                                        </summary>
                                        <div className="team-action-menu">
                                          {currentMembership?.role === "owner" && (
                                            <button
                                              type="button"
                                              onClick={() => {
                                                setRoleUpdate(
                                                  member.role === "admin" ? "member" : "admin",
                                                );
                                                setTeamAction({ kind: "role", member });
                                              }}
                                            >
                                              Change role
                                            </button>
                                          )}
                                          <button
                                            type="button"
                                            onClick={() => setTeamAction({ kind: "reset", member })}
                                          >
                                            Reset password
                                          </button>
                                          <button
                                            className="danger-action"
                                            type="button"
                                            onClick={() =>
                                              setTeamAction({ kind: "deactivate", member })
                                            }
                                          >
                                            Deactivate access
                                          </button>
                                        </div>
                                      </details>
                                    ) : (
                                      <span aria-label="No actions">—</span>
                                    )
                                  ) : (
                                    <span aria-label="No actions">—</span>
                                  )}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>
                  <div className="team-card">
                    <h3>Pending invitations</h3>
                    {teamInvitations.length === 0 ? (
                      <div className="compact-state">No pending invitations.</div>
                    ) : filteredTeamInvitations.length === 0 ? (
                      <div className="compact-state">No invitations match this search.</div>
                    ) : (
                      <div className="team-table-wrap">
                        <table className="team-table">
                          <colgroup>
                            <col className="team-col-member" />
                            <col className="team-col-role" />
                            <col className="team-col-status" />
                            <col className="team-col-date" />
                            <col className="team-col-actions" />
                          </colgroup>
                          <thead>
                            <tr>
                              <th>Email</th>
                              <th>Role</th>
                              <th aria-label="Status" />
                              <th>Expires</th>
                              <th aria-label={canManageTeam ? "Actions" : undefined}>
                                {canManageTeam ? "Actions" : null}
                              </th>
                            </tr>
                          </thead>
                          <tbody>
                          {filteredTeamInvitations.map((invitation) => (
                              <tr key={invitation.id}>
                                <td>
                                  {invitation.invited_email}
                                  {newLinkInvitationId === invitation.id && (
                                    <span className="team-status-badge">New link created</span>
                                  )}
                                </td>
                                <td>{teamRoleLabel(invitation.role)}</td>
                                <td />
                                <td>{new Date(invitation.expires_at).toLocaleDateString()}</td>
                                <td>
                                  {canManageTeam &&
                                    (currentMembership?.role === "owner" ||
                                      invitation.role === "member") && (
                                      <div className="inline-actions">
                                        <button
                                          className="text-button"
                                          type="button"
                                          disabled={teamLoading}
                                          aria-label={
                                            copiedInvitationId === invitation.id
                                              ? "Copied"
                                              : "Copy invitation link"
                                          }
                                          title={
                                            copiedInvitationId === invitation.id
                                              ? "Copied"
                                              : "Copy invitation link"
                                          }
                                          onClick={() =>
                                            invitationLink?.id === invitation.id
                                              ? void copyInvitationLink(
                                                  invitationLink.id,
                                                  invitationLink.url,
                                                )
                                              : void resendInvitation(invitation)
                                          }
                                        >
                                          {invitationLink?.id === invitation.id ? (
                                            copiedInvitationId === invitation.id ? (
                                              <CheckIcon />
                                            ) : (
                                              <CopyIcon />
                                            )
                                          ) : (
                                            "Create link"
                                          )}
                                        </button>
                                        <button
                                          className="text-button danger-action"
                                          type="button"
                                          disabled={teamLoading}
                                          onClick={() =>
                                            setTeamAction({ kind: "revoke", invitation })
                                          }
                                        >
                                          Cancel
                                        </button>
                                      </div>
                                    )}
                                </td>
                              </tr>
                            ))}
                          </tbody>
                        </table>
                      </div>
                    )}
                  </div>
                </>
              )}
              {inviteOpen && (
                <div className="dialog-backdrop" role="presentation">
                  <form
                    className="dialog-card"
                    onSubmit={inviteMember}
                    role="dialog"
                    aria-modal="true"
                    aria-labelledby="invite-heading"
                  >
                    <h2 id="invite-heading">Invite member</h2>
                    <p className="dialog-help">
                      Choose how this person will help with the workspace. Passwords remain private.
                    </p>
                    <label>
                      Email
                      <input
                        type="email"
                        value={inviteEmail}
                        onChange={(event) => setInviteEmail(event.target.value)}
                        required
                      />
                    </label>
                    <label>
                      Role
                      <select
                        value={inviteRole}
                        onChange={(event) => setInviteRole(event.target.value as typeof inviteRole)}
                      >
                        {canInviteAdmin && <option value="admin">Admin</option>}
                        <option value="member">Team Member</option>
                      </select>
                    </label>
                    <div className="dialog-actions">
                      <button
                        className="text-button"
                        type="button"
                        onClick={() => setInviteOpen(false)}
                      >
                        Cancel
                      </button>
                      <button className="primary-button" disabled={teamLoading}>
                        {teamLoading ? "Sending..." : "Send Invitation"}
                      </button>
                    </div>
                  </form>
                </div>
              )}
              {teamAction && (
                <div className="dialog-backdrop" role="presentation">
                  <div
                    className="dialog-card"
                    role="dialog"
                    aria-modal="true"
                    aria-labelledby="team-action-heading"
                  >
                    <h2 id="team-action-heading">
                      {teamAction.kind === "role" && "Change role"}
                      {teamAction.kind === "deactivate" && "Deactivate access"}
                      {teamAction.kind === "reset" && "Reset password"}
                      {teamAction.kind === "revoke" && "Cancel invitation"}
                    </h2>
                    {teamAction.kind === "role" && (
                      <>
                        <p>Update access for {teamAction.member.email}.</p>
                        <label>
                          New role
                          <select
                            value={roleUpdate}
                            onChange={(event) =>
                              setRoleUpdate(event.target.value as "admin" | "member")
                            }
                          >
                            <option value="admin">Admin</option>
                            <option value="member">Team Member</option>
                          </select>
                        </label>
                        <p className="dialog-help">
                          Admins manage Team Members. Team Members handle daily Invoxa work without
                          team administration.
                        </p>
                      </>
                    )}
                    {teamAction.kind === "deactivate" && (
                      <p>
                        This disables {teamAction.member.email}'s access to this workspace. Their
                        account and workspace data remain intact.
                      </p>
                    )}
                    {teamAction.kind === "reset" && (
                      <p>
                        The current password cannot be viewed. This starts the secure reset process
                        while keeping the password private.
                      </p>
                    )}
                    {teamAction.kind === "revoke" && (
                      <p>
                        This person will no longer be able to join this workspace using this
                        invitation. Existing accounts will not be deleted.
                      </p>
                    )}
                    <div className="dialog-actions">
                      <button
                        className="text-button"
                        type="button"
                        disabled={teamActionLoading}
                        onClick={() => setTeamAction(null)}
                      >
                        Cancel
                      </button>
                      <button
                        className={
                          teamAction.kind === "deactivate" || teamAction.kind === "revoke"
                            ? "danger-button"
                            : "primary-button"
                        }
                        type="button"
                        disabled={teamActionLoading}
                        onClick={() => void runTeamAction()}
                      >
                        {teamActionLoading
                          ? "Working..."
                          : teamAction.kind === "deactivate"
                            ? "Deactivate access"
                            : teamAction.kind === "revoke"
                              ? "Cancel invitation"
                              : teamAction.kind === "reset"
                                ? "Start reset"
                                : "Save role"}
                      </button>
                    </div>
                  </div>
                </div>
              )}
            </section>
          )}

          {workspaceView === "usage" && (
            <section className="phase-panel usage-page" aria-labelledby="usage-heading">
              {usageLoading ? (
                <div className="usage-loading" role="status">
                  Loading plan and usage...
                </div>
              ) : usageError ? (
                <div className="compact-state error-state" role="alert">
                  <p>{usageError}</p>
                  <button
                    className="secondary-button"
                    onClick={() => void loadUsage()}
                    type="button"
                  >
                    Retry
                  </button>
                </div>
              ) : usage ? (
                <>
                  <article className="usage-plan-card">
                    <div>
                      <p className="eyebrow">Current plan</p>
                      <h2 id="usage-heading">{usage.plan.name}</h2>
                      <p>Plan limits and usage for this workspace.</p>
                    </div>
                    <span className="usage-plan-code">{usage.plan.code}</span>
                  </article>
                  <div className="usage-grid">
                    <UsageMetric
                      label="Monthly processing"
                      used={usage.processing.used_pages}
                      limit={usage.processing.limit_pages}
                      remaining={usage.processing.remaining_pages}
                      unit="pages"
                    />
                    <UsageMetric
                      label="Active seats"
                      used={usage.seats.used}
                      limit={usage.seats.limit}
                      remaining={usage.seats.remaining}
                    />
                    <UsageMetric
                      label="Source storage"
                      used={usage.storage.used_bytes}
                      limit={usage.storage.limit_bytes}
                      remaining={usage.storage.remaining_bytes}
                      formatValue={formatUsageBytes}
                    />
                  </div>
                  <p className="usage-period">
                    Processing usage for {formatUsagePeriod(usage.processing.period)}.
                  </p>
                </>
              ) : (
                <div className="compact-state">Usage information is not available.</div>
              )}
            </section>
          )}

          {workspaceView === "overview" && (
            <section className="phase-panel" aria-labelledby="overview-heading">
              <div className="phase-heading">
                <div>
                  <p className="eyebrow">Executive summary</p>
                  <h2 id="overview-heading">Analytics overview</h2>
                </div>
                <label>
                  Time range
                  <select
                    value={analyticsRange}
                    onChange={(event) => setAnalyticsRange(event.target.value)}
                  >
                    <option value="7d">Last 7 days</option>
                    <option value="30d">Last 30 days</option>
                    <option value="90d">Last 90 days</option>
                    <option value="year">This year</option>
                    <option value="custom">Custom range</option>
                    <option value="all">All time</option>
                  </select>
                </label>
                {analyticsRange === "custom" && (
                  <div className="compact-filters custom-dates">
                    <label>
                      Start date
                      <input
                        type="date"
                        value={customStartDate}
                        onChange={(event) => setCustomStartDate(event.target.value)}
                      />
                    </label>
                    <label>
                      End date
                      <input
                        type="date"
                        value={customEndDate}
                        onChange={(event) => setCustomEndDate(event.target.value)}
                      />
                    </label>
                  </div>
                )}
              </div>
              {insightsLoading ? (
                <div className="compact-state" role="status">
                  Loading analytics...
                </div>
              ) : insightsError ? (
                <div className="compact-state error-state">
                  <p>{insightsError}</p>
                  <button className="secondary-button" onClick={() => void loadAnalytics()}>
                    Retry
                  </button>
                </div>
              ) : !analytics ? (
                <div className="compact-state">Analytics are not available yet.</div>
              ) : (
                <>
                  <div className="insight-kpis">
                    <div className="stat-card">
                      <span>Total invoices</span>
                      <strong>{analytics.kpis.total_invoices}</strong>
                    </div>
                    <div className="stat-card">
                      <span>Completed</span>
                      <strong>{analytics.kpis.completed_documents}</strong>
                    </div>
                    <div className="stat-card">
                      <span>Needs review</span>
                      <strong>{analytics.kpis.needs_review}</strong>
                    </div>
                    <div className="stat-card">
                      <span>Open AI findings</span>
                      <strong>{analytics.kpis.unresolved_ai_findings}</strong>
                    </div>
                    <div className="stat-card">
                      <span>High-risk findings</span>
                      <strong>{analytics.kpis.high_severity_ai_findings}</strong>
                    </div>
                  </div>
                  {analytics.kpis.currency_totals.length ? (
                    <div className="currency-strip" aria-label="Invoice values by currency">
                      {analytics.kpis.currency_totals.map((item) => (
                        <div key={item.currency}>
                          <span>{item.currency} total</span>
                          <strong>{formatMoney(item.total, item.currency)}</strong>
                          <small>
                            Average {formatMoney(item.average, item.currency)} · Tax{" "}
                            {formatMoney(item.tax, item.currency)}
                          </small>
                        </div>
                      ))}
                    </div>
                  ) : (
                    <div className="compact-state">No invoice values in this period.</div>
                  )}
                  <div className="analytics-grid">
                    <article className="chart-card">
                      <h3>Invoice count over time</h3>
                      {analytics.trends.invoice_count.length ? (
                        analytics.trends.invoice_count.map((point) => (
                          <div
                            className="bar-row"
                            key={point.period}
                            aria-label={`${point.period}: ${point.count} invoices`}
                          >
                            <span>{point.period}</span>
                            <div>
                              <i
                                style={{
                                  width: `${Math.max(4, Math.min(100, point.count * 10))}%`,
                                }}
                              />
                            </div>
                            <strong>{point.count}</strong>
                          </div>
                        ))
                      ) : (
                        <p className="muted-copy">No invoices in this period.</p>
                      )}
                    </article>
                    <article className="chart-card">
                      <h3>Top vendors</h3>
                      {analytics.trends.top_vendors.length ? (
                        analytics.trends.top_vendors.slice(0, 6).map((vendor) => (
                          <div className="vendor-row" key={`${vendor.vendor}-${vendor.currency}`}>
                            <span title={vendor.vendor}>{vendor.vendor}</span>
                            <strong>{formatMoney(vendor.total, vendor.currency)}</strong>
                          </div>
                        ))
                      ) : (
                        <p className="muted-copy">
                          Vendor spend will appear when invoices are available.
                        </p>
                      )}
                    </article>
                    <article className="chart-card">
                      <h3>AI findings by severity</h3>
                      {Object.entries(analytics.trends.findings_by_severity).map(
                        ([severity, count]) => (
                          <div className="vendor-row" key={severity}>
                            <span className={`finding-badge ${severity}`}>{severity}</span>
                            <strong>{count}</strong>
                          </div>
                        ),
                      )}
                      {!Object.keys(analytics.trends.findings_by_severity).length && (
                        <p className="muted-copy">No AI findings in this period.</p>
                      )}
                    </article>
                  </div>
                </>
              )}
            </section>
          )}

          {workspaceView === "ai-analysis" && (
            <section className="phase-panel" aria-labelledby="ai-heading">
              <div className="phase-heading">
                <div>
                  <p className="eyebrow">Review attention</p>
                  <h2 id="ai-heading">Review findings</h2>
                  <p>Statistical and machine-assisted checks with transparent evidence.</p>
                </div>
                <div className="compact-filters">
                  <label>
                    Severity
                    <select
                      aria-label="AI severity"
                      value={findingSeverity}
                      onChange={(event) => setFindingSeverity(event.target.value)}
                    >
                      <option value="">All</option>
                      <option value="low">Low</option>
                      <option value="medium">Medium</option>
                      <option value="high">High</option>
                    </select>
                  </label>
                  <label>
                    Status
                    <select
                      aria-label="AI finding status"
                      value={findingStatus}
                      onChange={(event) => setFindingStatus(event.target.value)}
                    >
                      <option value="">All</option>
                      <option value="open">Open</option>
                      <option value="acknowledged">Acknowledged</option>
                      <option value="resolved">Resolved</option>
                    </select>
                  </label>
                </div>
              </div>
              {insightsLoading ? (
                <div className="compact-state" role="status">
                  Loading AI findings...
                </div>
              ) : insightsError ? (
                <div className="compact-state error-state">
                  <p>{insightsError}</p>
                  <button className="secondary-button" onClick={() => void loadFindings()}>
                    Retry
                  </button>
                </div>
              ) : findings.length === 0 ? (
                <div className="compact-state">
                  <strong>No findings match these filters.</strong>
                  <p>Completed documents remain available in Documents.</p>
                </div>
              ) : (
                <div className="findings-layout">
                  <div className="table-scroll" tabIndex={0} aria-label="AI findings">
                    <table className="findings-table">
                      <thead>
                        <tr>
                          <th>Severity</th>
                          <th>Finding</th>
                          <th>Document</th>
                          <th>Status</th>
                          <th>Created</th>
                        </tr>
                      </thead>
                      <tbody>
                        {findings.map((finding) => (
                          <tr
                            key={finding.id}
                            onClick={() => setSelectedFinding(finding)}
                            className={selectedFinding?.id === finding.id ? "selected" : ""}
                          >
                            <td>
                              <span className={`finding-badge ${finding.severity}`}>
                                {finding.severity}
                              </span>
                            </td>
                            <td>
                              <button
                                className="table-link"
                                type="button"
                                onClick={() => setSelectedFinding(finding)}
                              >
                                {finding.title}
                              </button>
                              <small>{finding.category.replaceAll("_", " ")}</small>
                            </td>
                            <td title={finding.document_filename}>{finding.document_filename}</td>
                            <td>
                              <span className="finding-status">{finding.status}</span>
                            </td>
                            <td>{formatDate(finding.created_at)}</td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                  <aside className="finding-detail" aria-label="AI finding detail">
                    {!selectedFinding ? (
                      <div className="compact-state">Select a finding to inspect its evidence.</div>
                    ) : (
                      <>
                        <div>
                          <span className={`finding-badge ${selectedFinding.severity}`}>
                            {selectedFinding.severity}
                          </span>
                          <span className="finding-status">{selectedFinding.status}</span>
                        </div>
                        <h3>{selectedFinding.title}</h3>
                        <p>{selectedFinding.explanation}</p>
                        {(selectedFinding.observed_value || selectedFinding.expected_value) && (
                          <dl className="evidence-grid">
                            <div>
                              <dt>Observed</dt>
                              <dd>{selectedFinding.observed_value ?? "—"}</dd>
                            </div>
                            <div>
                              <dt>Expected</dt>
                              <dd>{selectedFinding.expected_value ?? "—"}</dd>
                            </div>
                          </dl>
                        )}
                        {selectedFinding.confidence && (
                          <p className="muted-copy">
                            Signal confidence:{" "}
                            {(Number(selectedFinding.confidence) * 100).toFixed(0)}%
                          </p>
                        )}
                        {selectedFinding.affected_fields.length > 0 && (
                          <p className="muted-copy">
                            Affected fields: {selectedFinding.affected_fields.join(", ")}
                          </p>
                        )}
                        {Object.keys(selectedFinding.evidence).length > 0 && (
                          <details className="finding-evidence">
                            <summary>Supporting evidence</summary>
                            <pre>{JSON.stringify(selectedFinding.evidence, null, 2)}</pre>
                          </details>
                        )}
                        <div className="detail-actions">
                          <button
                            className="secondary-button"
                            onClick={() => {
                              setWorkspaceView("documents");
                              void openDocument(selectedFinding.document_id);
                            }}
                          >
                            Open source document
                          </button>
                          {canMutateDocuments && selectedFinding.status === "open" && (
                            <button
                              className="secondary-button"
                              onClick={() =>
                                void updateFindingStatus(selectedFinding, "acknowledged")
                              }
                            >
                              Acknowledge
                            </button>
                          )}
                          {canMutateDocuments && selectedFinding.status !== "resolved" && (
                            <button
                              className="primary-button"
                              onClick={() => void updateFindingStatus(selectedFinding, "resolved")}
                            >
                              Resolve
                            </button>
                          )}
                          {canMutateDocuments && selectedFinding.status === "resolved" && (
                            <button
                              className="secondary-button"
                              onClick={() => void updateFindingStatus(selectedFinding, "open")}
                            >
                              Reopen
                            </button>
                          )}
                        </div>
                      </>
                    )}
                  </aside>
                </div>
              )}
            </section>
          )}

          {workspaceView === "reports" && (
            <section className="phase-panel" aria-labelledby="reports-heading">
              <div className="phase-heading">
                <div>
                  <p className="eyebrow">Server-generated data</p>
                  <h2 id="reports-heading">Reports</h2>
                  <p>Generate filtered, ownership-scoped operational reports.</p>
                </div>
              </div>
              <div className="report-controls">
                <label>
                  Report type
                  <select
                    value={reportType}
                    onChange={(event) => {
                      setReportType(event.target.value as ReportType);
                      setReport(null);
                    }}
                  >
                    <option value="financial">Financial summary</option>
                    <option value="ai-analysis">AI Analysis</option>
                    <option value="processing-quality">Processing quality</option>
                  </select>
                </label>
                <label>
                  Time range
                  <select
                    value={analyticsRange}
                    onChange={(event) => {
                      setAnalyticsRange(event.target.value);
                      setReport(null);
                    }}
                  >
                    <option value="7d">Last 7 days</option>
                    <option value="30d">Last 30 days</option>
                    <option value="90d">Last 90 days</option>
                    <option value="year">This year</option>
                    <option value="custom">Custom range</option>
                    <option value="all">All time</option>
                  </select>
                </label>
                {analyticsRange === "custom" && (
                  <>
                    <label>
                      Start date
                      <input
                        type="date"
                        value={customStartDate}
                        onChange={(event) => {
                          setCustomStartDate(event.target.value);
                          setReport(null);
                        }}
                      />
                    </label>
                    <label>
                      End date
                      <input
                        type="date"
                        value={customEndDate}
                        onChange={(event) => {
                          setCustomEndDate(event.target.value);
                          setReport(null);
                        }}
                      />
                    </label>
                  </>
                )}
                <button
                  className="primary-button"
                  disabled={
                    reportLoading ||
                    (analyticsRange === "custom" && (!customStartDate || !customEndDate))
                  }
                  onClick={() => void generateReport()}
                >
                  {reportLoading ? "Generating..." : "Generate report"}
                </button>
              </div>
              {insightsError && <div className="alert error">{insightsError}</div>}
              {report ? (
                <div className="report-preview">
                  <div className="phase-heading">
                    <h3>Report preview</h3>
                    <div className="detail-actions">
                      {(["csv", "xlsx", "json"] as const).map((format) => (
                        <button
                          className="secondary-button"
                          disabled={exporting !== null}
                          key={format}
                          onClick={() => void downloadReport(format)}
                        >
                          Export {format.toUpperCase()}
                        </button>
                      ))}
                    </div>
                  </div>
                  <ReportPreview report={report} reportType={reportType} />
                </div>
              ) : (
                !reportLoading && (
                  <div className="compact-state">
                    Choose a report and generate a current preview.
                  </div>
                )
              )}
            </section>
          )}

          <div className={workspaceView === "documents" ? "" : "view-hidden"}>
            <div className="stats">
              <div className="stat-card total">
                <span className="kpi-icon" aria-hidden="true">
                  ▤
                </span>
                <div>
                  <span>Total documents</span>
                  <strong>{documentTotal}</strong>
                  <small>All uploaded documents</small>
                </div>
              </div>

              <div className="stat-card processing">
                <span className="kpi-icon" aria-hidden="true">
                  ◌
                </span>
                <div>
                  <span>Processing</span>
                  <strong>
                    {
                      documents.filter(
                        (document) =>
                          document.status === "queued" || document.status === "processing",
                      ).length
                    }
                  </strong>
                  <small>Currently being analyzed</small>
                </div>
              </div>

              <div className="stat-card completed">
                <span className="kpi-icon" aria-hidden="true">
                  ✓
                </span>
                <div>
                  <span>Completed</span>
                  <strong>
                    {documents.filter((document) => document.status === "completed").length}
                  </strong>
                  <small>Successfully processed</small>
                </div>
              </div>

              <div className="stat-card failed">
                <span className="kpi-icon" aria-hidden="true">
                  !
                </span>
                <div>
                  <span>Failed</span>
                  <strong>
                    {documents.filter((document) => document.status === "failed").length}
                  </strong>
                  <small>Processing failed</small>
                </div>
              </div>
            </div>

            <div className="productivity-bar">
              <label className="search-field">
                <span className="visually-hidden">Search documents</span>
                <span className="search-icon" aria-hidden="true">
                  ⌕
                </span>
                <input
                  aria-label="Search documents"
                  value={searchInput}
                  onChange={(event) => setSearchInput(event.target.value)}
                  placeholder="Search by filename, invoice number, vendor, customer..."
                />
              </label>
              <label>
                Status
                <select
                  value={statusFilter}
                  onChange={(event) => {
                    setPage(1);
                    setStatusFilter(event.target.value);
                  }}
                >
                  <option value="">All statuses</option>
                  <option value="queued">Queued</option>
                  <option value="processing">Processing</option>
                  <option value="completed">Completed</option>
                  <option value="failed">Failed</option>
                </select>
              </label>
              <label>
                File type
                <select
                  value={mimeFilter}
                  onChange={(event) => {
                    setPage(1);
                    setMimeFilter(event.target.value);
                  }}
                >
                  <option value="">All file types</option>
                  <option value="application/pdf">PDF</option>
                  <option value="image/jpeg">JPG/JPEG</option>
                  <option value="image/png">PNG</option>
                </select>
              </label>
              <label>
                Sort
                <select
                  value={`${sortBy}:${sortDirection}`}
                  onChange={(event) => {
                    const [nextSort, nextDirection] = event.target.value.split(":");
                    setPage(1);
                    setSortBy(nextSort);
                    setSortDirection(nextDirection);
                  }}
                >
                  <option value="created_at:desc">Newest</option>
                  <option value="created_at:asc">Oldest</option>
                  <option value="filename:asc">Filename A–Z</option>
                  <option value="filename:desc">Filename Z–A</option>
                  <option value="invoice_date:desc">Invoice date</option>
                </select>
              </label>
            </div>

            <section className="workspace-grid">
              <div className="documents-panel">
                <div className="panel-header">
                  <div>
                    <h3>Documents</h3>
                    <p>{documentTotal} stored files</p>
                  </div>

                  <button
                    className="icon-button"
                    onClick={() => void loadDocuments()}
                    disabled={documentsLoading}
                    title="Refresh documents"
                  >
                    ↻
                  </button>
                </div>

                <div className="bulk-actions" aria-label="Bulk export controls">
                  <span>{selectedDocumentIds.size} selected</span>
                  <button
                    className="secondary-button"
                    disabled={selectedDocumentIds.size === 0 || exporting !== null}
                    onClick={() => void downloadBulkExport("csv")}
                  >
                    CSV
                  </button>
                  <button
                    className="secondary-button"
                    disabled={selectedDocumentIds.size === 0 || exporting !== null}
                    onClick={() => void downloadBulkExport("xlsx")}
                  >
                    XLSX
                  </button>
                  <button
                    className="secondary-button"
                    disabled={selectedDocumentIds.size === 0 || exporting !== null}
                    onClick={() => void downloadBulkExport("json")}
                  >
                    JSON
                  </button>
                </div>

                {documentsLoading ? (
                  <div className="empty-state">Loading documents...</div>
                ) : documents.length === 0 ? (
                  <div className="empty-state">
                    <strong>No documents yet.</strong>
                    <p>Upload your first PDF or image to begin.</p>
                  </div>
                ) : (
                  <div className="document-table-scroll" tabIndex={0} aria-label="Stored documents">
                    <table className="document-table">
                      <thead>
                        <tr>
                          <th className="select-column">
                            <span className="visually-hidden">Select</span>
                          </th>
                          <th>Name</th>
                          <th>Date added</th>
                          <th>Type</th>
                          <th>Status</th>
                          <th>Actions</th>
                        </tr>
                      </thead>
                      <tbody>
                        {documents.map((document) => (
                          <tr
                            className={selectedDocument?.id === document.id ? "selected" : ""}
                            key={document.id}
                          >
                            <td>
                              <label
                                className="table-selection"
                                aria-label={`Select ${document.original_filename}`}
                              >
                                <input
                                  type="checkbox"
                                  checked={selectedDocumentIds.has(document.id)}
                                  onChange={(event) =>
                                    setSelectedDocumentIds((current) => {
                                      const next = new Set(current);
                                      if (event.target.checked) next.add(document.id);
                                      else next.delete(document.id);
                                      return next;
                                    })
                                  }
                                />
                              </label>
                            </td>
                            <td>
                              <button
                                className="document-name"
                                onClick={() => void openDocument(document.id)}
                                type="button"
                              >
                                <span
                                  className={`file-icon ${document.mime_type === "application/pdf" ? "pdf" : "image"}`}
                                >
                                  {document.mime_type === "application/pdf" ? "PDF" : "IMG"}
                                </span>
                                <span className="document-info">
                                  <strong title={document.original_filename}>
                                    {document.original_filename}
                                  </strong>
                                  <small>{formatFileSize(document.file_size)}</small>
                                </span>
                              </button>
                            </td>
                            <td>{formatDate(document.created_at)}</td>
                            <td>
                              <span className="type-badge">
                                {document.mime_type === "application/pdf"
                                  ? "PDF"
                                  : document.mime_type.split("/")[1]?.toUpperCase()}
                              </span>
                            </td>
                            <td>
                              <span className={`status ${document.status}`}>
                                {statusLabel(document.status)}
                              </span>
                            </td>
                            <td>
                              {canMutateDocuments ? (
                                <div className="row-actions">
                                  <button
                                    onClick={() => void reprocessDocument(document.id)}
                                    disabled={
                                      document.status === "queued" ||
                                      document.status === "processing"
                                    }
                                  >
                                    Reprocess
                                  </button>
                                  <button
                                    className="danger-button"
                                    onClick={() => setDeleteTarget(document)}
                                  >
                                    Delete
                                  </button>
                                </div>
                              ) : (
                                <span className="muted-copy">View only</span>
                              )}
                            </td>
                          </tr>
                        ))}
                      </tbody>
                    </table>
                  </div>
                )}
                <div className="pagination" aria-label="Document pages">
                  <button
                    className="secondary-button"
                    disabled={page <= 1 || documentsLoading}
                    onClick={() => setPage((current) => current - 1)}
                  >
                    Previous
                  </button>
                  <span>
                    Page {page} of {Math.max(totalPages, 1)}
                  </span>
                  <button
                    className="secondary-button"
                    disabled={page >= totalPages || documentsLoading}
                    onClick={() => setPage((current) => current + 1)}
                  >
                    Next
                  </button>
                </div>
              </div>

              <aside
                className={`details-panel ${!selectedDocument && !detailLoading ? "is-empty" : ""}`}
              >
                {detailLoading && !selectedDocument ? (
                  <div className="details-empty" role="status">
                    Loading document details...
                  </div>
                ) : !selectedDocument ? (
                  <div className="details-empty">
                    <div className="large-file-icon">⌁</div>
                    <h3>Select a document</h3>
                    <p>
                      Choose a document from the list to inspect its extracted text and processing
                      state.
                    </p>
                  </div>
                ) : (
                  <>
                    <div className="panel-header">
                      <div>
                        <p className="eyebrow">Document detail</p>
                        <h3>{selectedDocument.original_filename}</h3>
                      </div>

                      <span className={`status ${selectedDocument.status}`}>
                        {statusLabel(selectedDocument.status)}
                      </span>
                    </div>

                    <div className="detail-meta">
                      <div>
                        <span>File type</span>
                        <strong>{selectedDocument.mime_type}</strong>
                      </div>

                      <div>
                        <span>Size</span>
                        <strong>{formatFileSize(selectedDocument.file_size)}</strong>
                      </div>

                      <div>
                        <span>Created</span>
                        <strong>{formatDate(selectedDocument.created_at)}</strong>
                      </div>
                    </div>

                    <div className="detail-actions">
                      <button
                        className="secondary-button"
                        onClick={() => void downloadExport(selectedDocument.id, "csv")}
                        disabled={selectedDocument.status !== "completed" || exporting !== null}
                      >
                        {exporting === "csv" ? "Exporting..." : "Export CSV"}
                      </button>

                      <button
                        className="secondary-button"
                        onClick={() => void downloadExport(selectedDocument.id, "xlsx")}
                        disabled={selectedDocument.status !== "completed" || exporting !== null}
                      >
                        {exporting === "xlsx" ? "Exporting..." : "Export XLSX"}
                      </button>
                      <button
                        className="secondary-button"
                        onClick={() => void downloadExport(selectedDocument.id, "json")}
                        disabled={selectedDocument.status !== "completed" || exporting !== null}
                      >
                        {exporting === "json" ? "Exporting..." : "Export JSON"}
                      </button>
                    </div>

                    <section className="preview-section" aria-labelledby="preview-heading">
                      <div className="section-heading">
                        <h4 id="preview-heading">Original document</h4>
                      </div>
                      {previewLoading ? (
                        <div className="compact-state" role="status">
                          Loading secure preview...
                        </div>
                      ) : previewError ? (
                        <div className="compact-state error-state">
                          <p>{previewError}</p>
                          <button
                            className="secondary-button"
                            onClick={() => void loadPreview(selectedDocument.id)}
                          >
                            Retry preview
                          </button>
                        </div>
                      ) : selectedDocument.mime_type === "application/pdf" && previewUrl ? (
                        <iframe
                          className="document-preview"
                          src={previewUrl}
                          title={`Preview of ${selectedDocument.original_filename}`}
                        />
                      ) : selectedDocument.mime_type.startsWith("image/") && previewUrl ? (
                        <div className="image-preview-wrap">
                          <img
                            className="image-preview"
                            src={previewUrl}
                            alt={`Preview of ${selectedDocument.original_filename}`}
                          />
                        </div>
                      ) : (
                        <div className="compact-state">
                          Preview is unavailable for this file type.
                        </div>
                      )}
                    </section>

                    <section className="invoice-section" aria-labelledby="invoice-heading">
                      <div className="section-heading">
                        <h4 id="invoice-heading">Invoice results</h4>
                        {selectedDocument.invoice_extraction && (
                          <span
                            className={`validation ${selectedDocument.invoice_extraction.is_valid ? "valid" : "invalid"}`}
                          >
                            {selectedDocument.invoice_extraction.is_valid
                              ? "Validated"
                              : "Needs review"}
                          </span>
                        )}
                      </div>
                      {selectedDocument.invoice_extraction ? (
                        <>
                          {selectedDocument.invoice_extraction.validation_error && (
                            <div className="inline-warning">
                              {selectedDocument.invoice_extraction.validation_error}
                            </div>
                          )}
                          <dl className="invoice-fields">
                            <div>
                              <dt>Invoice number</dt>
                              <dd>
                                {displayValue(selectedDocument.invoice_extraction.invoice_number)}
                              </dd>
                            </div>
                            <div>
                              <dt>Invoice date</dt>
                              <dd>
                                {displayValue(selectedDocument.invoice_extraction.invoice_date)}
                              </dd>
                            </div>
                            <div>
                              <dt>Vendor</dt>
                              <dd>
                                {displayValue(selectedDocument.invoice_extraction.vendor_name)}
                              </dd>
                            </div>
                            <div>
                              <dt>Customer</dt>
                              <dd>
                                {displayValue(selectedDocument.invoice_extraction.customer_name)}
                              </dd>
                            </div>
                            <div>
                              <dt>Subtotal</dt>
                              <dd>
                                {formatMoney(
                                  selectedDocument.invoice_extraction.subtotal,
                                  selectedDocument.invoice_extraction.currency,
                                )}
                              </dd>
                            </div>
                            <div>
                              <dt>Tax</dt>
                              <dd>
                                {formatMoney(
                                  selectedDocument.invoice_extraction.tax,
                                  selectedDocument.invoice_extraction.currency,
                                )}
                              </dd>
                            </div>
                            <div className="total-field">
                              <dt>Total</dt>
                              <dd>
                                {formatMoney(
                                  selectedDocument.invoice_extraction.total,
                                  selectedDocument.invoice_extraction.currency,
                                )}
                              </dd>
                            </div>
                          </dl>
                          {selectedDocument.invoice_extraction.line_items.length > 0 ? (
                            <div
                              className="table-scroll"
                              tabIndex={0}
                              aria-label="Invoice line items"
                            >
                              <table>
                                <thead>
                                  <tr>
                                    <th>Description</th>
                                    <th>Qty</th>
                                    <th>Unit price</th>
                                    <th>Total</th>
                                  </tr>
                                </thead>
                                <tbody>
                                  {selectedDocument.invoice_extraction.line_items.map((item) => (
                                    <tr key={item.position}>
                                      <td>{item.description}</td>
                                      <td>{item.quantity}</td>
                                      <td>
                                        {formatMoney(
                                          item.unit_price,
                                          selectedDocument.invoice_extraction?.currency ?? null,
                                        )}
                                      </td>
                                      <td>
                                        {formatMoney(
                                          item.line_total,
                                          selectedDocument.invoice_extraction?.currency ?? null,
                                        )}
                                      </td>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            </div>
                          ) : (
                            <p className="muted-copy">No line items were extracted.</p>
                          )}
                        </>
                      ) : (
                        <div className="compact-state">
                          No structured invoice fields were extracted.
                        </div>
                      )}
                    </section>

                    <section
                      className="analysis-results-section"
                      aria-labelledby="financial-validation-heading"
                    >
                      <div className="section-heading">
                        <div>
                          <h4 id="financial-validation-heading">Financial Validation</h4>
                          <p className="section-supporting-copy">
                            Arithmetic and invoice data consistency checks.
                          </p>
                        </div>
                        {documentAnalysis && (
                          <span
                            className={`analysis-status ${(documentAnalysis.financial_validation_status ?? "WARNING").toLowerCase()}`}
                          >
                            {documentAnalysis.financial_validation_status ?? "WARNING"}
                          </span>
                        )}
                      </div>
                      {documentAnalysisLoading ? (
                        <div className="compact-state" role="status">
                          Loading financial validation...
                        </div>
                      ) : documentAnalysisError ? (
                        <div className="compact-state error-state">
                          <p>{documentAnalysisError}</p>
                          <button
                            className="secondary-button"
                            type="button"
                            onClick={() => void loadDocumentAnalysis(selectedDocument.id)}
                          >
                            Retry analysis
                          </button>
                        </div>
                      ) : !documentAnalysis ? (
                        <div className="compact-state">
                          Financial validation is not available yet.
                        </div>
                      ) : documentAnalysis.analysis_status === "pending" ? (
                        <div className="compact-state">
                          Analysis is pending while this document finishes processing.
                        </div>
                      ) : documentAnalysis.analysis_status === "failed" ? (
                        <div className="compact-state error-state">
                          {documentAnalysis.analysis_error || "Analysis could not be completed."}
                        </div>
                      ) : (
                        <>
                          <div className="analysis-counts" aria-label="Financial validation counts">
                            <span>
                              {documentAnalysis.financial_validation_counts?.passed ?? 0} passed
                            </span>
                            <span>
                              {documentAnalysis.financial_validation_counts?.warning ?? 0} warnings
                            </span>
                            <span>
                              {documentAnalysis.financial_validation_counts?.failed ?? 0} failed
                            </span>
                          </div>
                          {(() => {
                            const financialFindings = (documentAnalysis.findings ?? []).filter(
                              (finding) => FINANCIAL_FINDING_CATEGORIES.has(finding.category),
                            );
                            return financialFindings.length > 0 ? (
                              <div className="analysis-finding-list">
                                {financialFindings.map((finding) => (
                                  <article className="analysis-finding" key={finding.id}>
                                    <div className="analysis-finding-heading">
                                      <strong>{finding.title}</strong>
                                      <span className={`severity-badge ${finding.severity}`}>
                                        {finding.severity}
                                      </span>
                                    </div>
                                    <p>{finding.explanation}</p>
                                    {finding.affected_fields.length > 0 && (
                                      <small>
                                        Affected fields: {finding.affected_fields.join(", ")}
                                      </small>
                                    )}
                                    {(finding.observed_value || finding.expected_value) && (
                                      <div className="analysis-values">
                                        {finding.observed_value && (
                                          <span>Observed: {finding.observed_value}</span>
                                        )}
                                        {finding.expected_value && (
                                          <span>Expected: {finding.expected_value}</span>
                                        )}
                                      </div>
                                    )}
                                  </article>
                                ))}
                              </div>
                            ) : (
                              <p className="muted-copy">No financial consistency issues found.</p>
                            );
                          })()}
                        </>
                      )}
                    </section>

                    <section
                      className="analysis-results-section"
                      aria-labelledby="anomaly-detection-heading"
                    >
                      <div className="section-heading">
                        <div>
                          <h4 id="anomaly-detection-heading">Anomaly Detection</h4>
                          <p className="section-supporting-copy">
                            Duplicate and unusual-pattern signals for review.
                          </p>
                        </div>
                        {documentAnalysis && (
                          <span className="analysis-status neutral">
                            {documentAnalysis.risk_level === "clear" ? "CLEAR" : "REVIEW"}
                          </span>
                        )}
                      </div>
                      {!documentAnalysis || documentAnalysisLoading ? (
                        <div className="compact-state">
                          {documentAnalysisLoading
                            ? "Loading anomaly signals..."
                            : "Anomaly analysis is not available yet."}
                        </div>
                      ) : documentAnalysis.analysis_status === "failed" ? (
                        <div className="compact-state error-state">
                          Anomaly analysis could not be completed.
                        </div>
                      ) : (
                        (() => {
                          const anomalyFindings = (documentAnalysis.findings ?? []).filter(
                            (finding) => !FINANCIAL_FINDING_CATEGORIES.has(finding.category),
                          );
                          return anomalyFindings.length > 0 ? (
                            <div className="analysis-finding-list">
                              {anomalyFindings.map((finding) => (
                                <article className="analysis-finding" key={finding.id}>
                                  <div className="analysis-finding-heading">
                                    <strong>{findingDisplayLabel(finding)}</strong>
                                    <span className={`severity-badge ${finding.severity}`}>
                                      {finding.severity}
                                    </span>
                                  </div>
                                  <p>{finding.explanation}</p>
                                  <div className="analysis-finding-meta">
                                    <span>Status: {finding.status}</span>
                                    {finding.confidence && (
                                      <span>Confidence: {finding.confidence}</span>
                                    )}
                                  </div>
                                  {(finding.observed_value || finding.expected_value) && (
                                    <div className="analysis-values">
                                      {finding.observed_value && (
                                        <span>Observed: {finding.observed_value}</span>
                                      )}
                                      {finding.expected_value && (
                                        <span>Baseline: {finding.expected_value}</span>
                                      )}
                                    </div>
                                  )}
                                </article>
                              ))}
                            </div>
                          ) : (
                            <p className="muted-copy">
                              No anomaly signals detected. No review is currently recommended.
                            </p>
                          );
                        })()
                      )}
                    </section>

                    <section className="extracted-section">
                      <div className="section-heading">
                        <h4>Extracted text</h4>
                      </div>

                      {selectedDocument.extracted_text ? (
                        <pre>{selectedDocument.extracted_text}</pre>
                      ) : selectedDocument.status === "failed" ? (
                        <div className="empty-state">
                          <strong>Processing failed.</strong>
                          <p>
                            {selectedDocument.error_details ||
                              "The processor did not provide an error detail."}
                          </p>
                        </div>
                      ) : (
                        <div className="empty-state">
                          <strong>No extracted text yet.</strong>
                          <p>
                            The document must finish processing before extracted content is
                            available.
                          </p>
                        </div>
                      )}
                    </section>
                  </>
                )}
              </aside>
            </section>
          </div>
        </section>
      </div>
      {deleteTarget && (
        <div className="modal-backdrop" role="presentation">
          <div
            className="confirmation-modal"
            role="dialog"
            aria-modal="true"
            aria-labelledby="delete-dialog-title"
            aria-describedby="delete-dialog-description"
            onKeyDown={containDeleteDialogFocus}
          >
            <h2 id="delete-dialog-title">Delete document?</h2>
            <p className="modal-filename">{deleteTarget.original_filename}</p>
            <p id="delete-dialog-description">
              This action permanently deletes this document and its associated analysis. This action
              cannot be undone.
            </p>
            <div className="modal-actions">
              <button
                className="secondary-button"
                ref={deleteCancelRef}
                onClick={() => setDeleteTarget(null)}
                type="button"
              >
                Cancel
              </button>
              <button
                className="danger-button"
                onClick={() => {
                  const documentId = deleteTarget.id;
                  setDeleteTarget(null);
                  void deleteDocument(documentId);
                }}
                type="button"
              >
                Delete document
              </button>
            </div>
          </div>
        </div>
      )}
    </main>
  );
}

const rootElement = document.getElementById("root");

if (rootElement) {
  ReactDOM.createRoot(rootElement).render(
    <React.StrictMode>
      {window.location.pathname === "/confirm-email-change" ? <EmailChangeConfirmation /> :
        window.location.pathname === "/reactivate-account" ? <AccountRecovery /> : <App />}
    </React.StrictMode>,
  );
}
