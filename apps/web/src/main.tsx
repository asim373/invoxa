import React, { useEffect, useRef, useState } from "react";
import ReactDOM from "react-dom/client";
import "./styles.css";
import {
  AUTHENTICATION_LOST_EVENT,
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

export function App() {
  const [token, setToken] = useState(getStoredToken);
  const [user, setUser] = useState<User | null>(null);

  const resetToken = new URLSearchParams(window.location.search).get("token") ?? "";
  const [authMode, setAuthMode] = useState<AuthMode>(resetToken ? "reset" : "login");
  const [email, setEmail] = useState("");
  const [password, setPassword] = useState("");
  const [confirmPassword, setConfirmPassword] = useState("");

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

  const [error, setError] = useState("");
  const [success, setSuccess] = useState("");

  const fileInputRef = useRef<HTMLInputElement | null>(null);
  const requestedDocumentId = useRef<string | null>(null);

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

  if (!token || !user) {
    return (
      <main className="auth-page">
        <section className="auth-card">
          <div className="brand-mark">DIA</div>

          <p className="eyebrow">Document intelligence</p>

          <h1>Document & Invoice Analyzer</h1>

          <p className="auth-intro">
            Upload business documents, process them, review extracted information, and export
            structured invoice data.
          </p>

          {authMode === "forgot" || authMode === "reset" ? (
            <>
              <h2>{authMode === "forgot" ? "Forgot password" : "Reset password"}</h2>
              <p className="auth-intro">
                {authMode === "forgot"
                  ? "Enter your account email and we’ll send password reset instructions."
                  : "Choose a new password with at least 12 characters."}
              </p>
            </>
          ) : (
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
          )}

          {authMode === "forgot" ? (
            <form onSubmit={handleForgotPassword} className="auth-form">
              <label>
                Email
                <input
                  type="email"
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
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
                  type="password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
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
              <label>
                Email
                <input
                  type="email"
                  value={email}
                  onChange={(event) => setEmail(event.target.value)}
                  placeholder="you@example.com"
                  required
                />
              </label>

              <label>
                Password
                <input
                  type="password"
                  value={password}
                  onChange={(event) => setPassword(event.target.value)}
                  placeholder={authMode === "register" ? "Minimum 12 characters" : "Your password"}
                  minLength={authMode === "register" ? 12 : 1}
                  required
                />
              </label>

              {error && <div className="alert error">{error}</div>}
              {success && <div className="alert success">{success}</div>}

              <button className="primary-button" disabled={loading}>
                {loading ? "Please wait..." : authMode === "login" ? "Sign in" : "Create account"}
              </button>
              {authMode === "login" && (
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
              )}
            </form>
          )}

          <p className="security-note">
            Authentication is protected with bearer tokens and password hashing.
          </p>
        </section>
      </main>
    );
  }

  return (
    <main className="app-shell">
      <header className="topbar">
        <div>
          <p className="eyebrow">Workspace</p>
          <h1>Document & Invoice Analyzer</h1>
        </div>

        <div className="user-area">
          <span>{user.email}</span>
          <button onClick={logout} className="secondary-button">
            Sign out
          </button>
        </div>
      </header>

      <section className="dashboard">
        <div className="dashboard-heading">
          <div>
            <p className="eyebrow">Your documents</p>
            <h2>Analysis workspace</h2>
            <p>Upload invoices and business documents for extraction and review.</p>
          </div>

          {canMutateDocuments && (
            <label className="upload-button">
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

        <div className="stats">
          <div className="stat-card">
            <span>Total documents</span>
            <strong>{documentTotal}</strong>
          </div>

          <div className="stat-card">
            <span>Processing on page</span>
            <strong>
              {
                documents.filter(
                  (document) => document.status === "queued" || document.status === "processing",
                ).length
              }
            </strong>
          </div>

          <div className="stat-card">
            <span>Completed on page</span>
            <strong>
              {documents.filter((document) => document.status === "completed").length}
            </strong>
          </div>

          <div className="stat-card">
            <span>Failed on page</span>
            <strong>{documents.filter((document) => document.status === "failed").length}</strong>
          </div>
        </div>

        <form
          className="productivity-bar"
          onSubmit={(event) => {
            event.preventDefault();
            setPage(1);
            setSearch(searchInput.trim());
          }}
        >
          <label>
            Search documents
            <input
              value={searchInput}
              onChange={(event) => setSearchInput(event.target.value)}
              placeholder="Filename, invoice, vendor, customer"
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
          <button className="secondary-button" type="submit">
            Search
          </button>
        </form>

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
              <div className="document-list">
                {documents.map((document) => (
                  <article
                    className={`document-row ${
                      selectedDocument?.id === document.id ? "selected" : ""
                    }`}
                    key={document.id}
                  >
                    <label
                      className="selection-control"
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
                    <button
                      className="document-main"
                      onClick={() => void openDocument(document.id)}
                    >
                      <div className="file-icon">
                        {document.mime_type === "application/pdf" ? "PDF" : "IMG"}
                      </div>

                      <div className="document-info">
                        <strong>{document.original_filename}</strong>
                        <span>
                          {formatFileSize(document.file_size)} · {formatDate(document.created_at)}
                        </span>
                      </div>

                      <span className={`status ${document.status}`}>
                        {statusLabel(document.status)}
                      </span>
                    </button>

                    {canMutateDocuments && (
                      <div className="row-actions">
                        <button
                          onClick={() => void reprocessDocument(document.id)}
                          disabled={
                            document.status === "queued" || document.status === "processing"
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
                    )}
                  </article>
                ))}
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

          <aside className="details-panel">
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
                    <div className="compact-state">Preview is unavailable for this file type.</div>
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
                          <dd>{displayValue(selectedDocument.invoice_extraction.invoice_date)}</dd>
                        </div>
                        <div>
                          <dt>Vendor</dt>
                          <dd>{displayValue(selectedDocument.invoice_extraction.vendor_name)}</dd>
                        </div>
                        <div>
                          <dt>Customer</dt>
                          <dd>{displayValue(selectedDocument.invoice_extraction.customer_name)}</dd>
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
                        <div className="table-scroll" tabIndex={0} aria-label="Invoice line items">
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
                        The document must finish processing before extracted content is available.
                      </p>
                    </div>
                  )}
                </section>
              </>
            )}
          </aside>
        </section>
      </section>
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
