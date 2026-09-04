import React, { useEffect, useRef, useState } from "react";
import ReactDOM from "react-dom/client";
import "./styles.css";
import {
  AUTHENTICATION_LOST_EVENT,
  API_BASE_URL,
  AuthenticationLostError,
  apiRequest,
  authenticatedFetch,
  clearStoredToken,
  getStoredToken,
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
};

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
type WorkspaceView = "overview" | "ai-analysis" | "reports" | "documents";

type AuthMode = "login" | "register" | "forgot" | "reset";

export const DOCUMENT_POLL_INTERVAL_MS = 1500;

function formatFileSize(bytes: number) {
  if (bytes < 1024) return `${bytes} B`;
  if (bytes < 1024 * 1024) return `${(bytes / 1024).toFixed(1)} KB`;
  return `${(bytes / (1024 * 1024)).toFixed(1)} MB`;
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

const workspaceLabels: Record<WorkspaceView, { title: string; description: string }> = {
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
};

function NavigationIcon({ view }: { view: WorkspaceView }) {
  const paths: Record<WorkspaceView, React.ReactNode> = {
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

export function App() {
  const [token, setToken] = useState(getStoredToken);
  const [user, setUser] = useState<User | null>(null);

  const resetToken = new URLSearchParams(window.location.search).get("token") ?? "";
  const googleLoginCode = new URLSearchParams(window.location.search).get("google_code") ?? "";
  const googleAuthError = new URLSearchParams(window.location.search).get("google_error") ?? "";
  const [authMode, setAuthMode] = useState<AuthMode>(resetToken ? "reset" : "login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");
  const [showPassword, setShowPassword] = useState(false);
  const [googleEnabled, setGoogleEnabled] = useState(false);
  const [googleLoading, setGoogleLoading] = useState(Boolean(googleLoginCode));

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

  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const requestedDocumentId = useRef<string | null>(null);

  useEffect(() => {
    if (token || resetToken) return;
    void apiRequest<{ enabled: boolean }>("/auth/google/config")
      .then((result) => setGoogleEnabled(result.enabled))
      .catch(() => setGoogleEnabled(false));
  }, [resetToken, token]);

  useEffect(() => {
    if (token || resetToken) return;
    if (googleAuthError) {
      const messages: Record<string, string> = {
        cancelled: "Google sign-in was cancelled.",
        invalid_state: "Google sign-in session expired. Please try again.",
        account_unavailable: "This account is not available for sign-in.",
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
  }, [googleAuthError, googleLoginCode, resetToken, token]);

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
      setUser(currentUser);
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
      if (authMode === "register") {
        await apiRequest<User>("/auth/register", {
          method: "POST",
          body: JSON.stringify({
            email,
            password,
          }),
        });

        setSuccess("Account created. You can now sign in.");
        setAuthMode("login");
        setPassword("");
      } else {
        const data = await apiRequest<{ access_token: string }>("/auth/login", {
          method: "POST",
          body: JSON.stringify({
            email,
            password,
          }),
        });

        setStoredToken(data.access_token);
        setToken(data.access_token);
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

  function logout() {
    clearStoredToken();
    setToken(null);
    setUser(null);
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
    setError("");
    setDetailLoading(true);

    try {
      const data = await apiRequest<DocumentDetail>(`/documents/${documentId}`);

      if (requestedDocumentId.current !== documentId) return;
      setSelectedDocument(data);
      await loadPreview(documentId);
    } catch (requestError) {
      if (requestError instanceof AuthenticationLostError) return;
      setError(requestError instanceof Error ? requestError.message : "Unable to open document.");
    } finally {
      setDetailLoading(false);
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
    const confirmed = window.confirm("Delete this document permanently?");

    if (!confirmed) return;

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

  if (!token || !user) {
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
                  : "Reset your password"}
          </h1>

          <p className="auth-intro">
            {authMode === "login" || authMode === "register"
              ? "Upload and process business documents, review extracted information with AI, and export structured invoice data."
              : authMode === "forgot"
                ? "Enter your account email and we’ll send password reset instructions."
                : "Choose a new password with at least 12 characters."}
          </p>

          {googleLoading && (
            <div className="alert" role="status">
              Completing Google sign-in…
            </div>
          )}

          {authMode === "login" || authMode === "register" ? (
            <div className="auth-tabs">
              <button
                className={authMode === "login" ? "active" : ""}
                onClick={() => {
                  setAuthMode("login");
                  setError("");
                  setSuccess("");
                }}
                type="button"
              >
                Sign in
              </button>

              <button
                className={authMode === "register" ? "active" : ""}
                onClick={() => {
                  setAuthMode("register");
                  setError("");
                  setSuccess("");
                }}
                type="button"
              >
                Create account
              </button>
            </div>
          ) : null}

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
                {loading ? "Sending..." : "Send reset instructions"}
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
          ) : (
            <form onSubmit={handleAuth} className="auth-form">
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
              {authMode === "login" && (
                <>
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
                </>
              )}
            </form>
          )}

          <p className="security-note">
            <span aria-hidden="true">◇</span> Secure. Private. Built for your business.
          </p>
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
          {(["overview", "ai-analysis", "reports", "documents"] as WorkspaceView[]).map((view) => (
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
              <small>{user.role} workspace</small>
            </div>
          </div>
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
              <h1>{workspaceLabels[workspaceView].title}</h1>
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
                  <h2 id="ai-heading">AI Analysis</h2>
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
                  <pre>{JSON.stringify(report, null, 2)}</pre>
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
                                    onClick={() => void deleteDocument(document.id)}
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
    </main>
  );
}

const rootElement = document.getElementById("root");

if (rootElement) {
  ReactDOM.createRoot(rootElement).render(
    <React.StrictMode>
      <App />
    </React.StrictMode>,
  );
}
