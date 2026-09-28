import { act, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { App, DOCUMENT_POLL_INTERVAL_MS } from "./main";

const id = "00000000-0000-4000-8000-000000000001";
const timestamp = "2026-09-02T00:00:00Z";
type ActiveStatus = "queued" | "processing" | "completed";

function response(body: unknown): Response {
  return new Response(JSON.stringify(body), {
    headers: { "Content-Type": "application/json" },
  });
}

function item(status: ActiveStatus) {
  return {
    id,
    original_filename: "invoice.pdf",
    mime_type: "application/pdf",
    file_size: 100,
    status,
    created_at: timestamp,
    updated_at: timestamp,
  };
}

const user = {
  id: "user-1",
  email: "test@example.com",
  is_active: true,
  role: "reviewer",
  created_at: timestamp,
  updated_at: timestamp,
};

async function flush() {
  await act(async () => {
    await Promise.resolve();
    await Promise.resolve();
    await Promise.resolve();
  });
}

describe("document live status", () => {
  beforeEach(() => {
    vi.useFakeTimers();
    localStorage.setItem("document_analyzer_token", "test-token");
    URL.createObjectURL = vi.fn(() => "blob:secure-preview");
    URL.revokeObjectURL = vi.fn();
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it("polls queued/processing documents and stops after completion", async () => {
    const intervalSpy = vi.spyOn(window, "setInterval");
    const statuses: ActiveStatus[] = ["queued", "processing", "completed"];
    let listRequest = 0;
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      if (String(input).endsWith("/auth/me")) return Promise.resolve(response(user));
      const status = statuses[Math.min(listRequest++, statuses.length - 1)];
      return Promise.resolve(
        response({ items: [item(status)], total: 1, page: 1, page_size: 100, total_pages: 1 }),
      );
    });

    render(<App />);
    await flush();
    expect(screen.getAllByText("Queued").length).toBeGreaterThan(0);
    expect(
      intervalSpy.mock.calls.filter(([, delay]) => delay === DOCUMENT_POLL_INTERVAL_MS),
    ).toHaveLength(1);

    await act(async () => vi.advanceTimersByTimeAsync(DOCUMENT_POLL_INTERVAL_MS));
    await flush();
    expect(screen.getAllByText("Processing").length).toBeGreaterThan(0);

    await act(async () => vi.advanceTimersByTimeAsync(DOCUMENT_POLL_INTERVAL_MS));
    await flush();
    expect(screen.getAllByText("Completed").length).toBeGreaterThan(0);
    expect(
      fetchMock.mock.calls.filter(([input]) => String(input).includes("page_size=20")),
    ).toHaveLength(3);
    await act(async () => vi.advanceTimersByTimeAsync(DOCUMENT_POLL_INTERVAL_MS * 2));
    await flush();
    expect(
      fetchMock.mock.calls.filter(([input]) => String(input).includes("page_size=20")),
    ).toHaveLength(3);
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "POST")).toBe(false);
  });

  it("refreshes selected details when polling observes a terminal state", async () => {
    let listRequest = 0;
    let detailStatus: "queued" | "completed" = "queued";
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.endsWith("/auth/me")) return Promise.resolve(response(user));
      if (url.endsWith(`/documents/${id}`)) {
        return Promise.resolve(
          response({
            ...item(detailStatus),
            extracted_text: detailStatus === "completed" ? "Persisted final text" : null,
          }),
        );
      }
      const status = listRequest++ === 0 ? "queued" : "completed";
      if (status === "completed") detailStatus = "completed";
      return Promise.resolve(
        response({ items: [item(status)], total: 1, page: 1, page_size: 100, total_pages: 1 }),
      );
    });

    render(<App />);
    await flush();
    fireEvent.click(screen.getByRole("button", { name: /invoice\.pdf/i }));
    await flush();
    await act(async () => vi.advanceTimersByTimeAsync(DOCUMENT_POLL_INTERVAL_MS));
    await flush();
    await flush();

    expect(screen.getByText("Persisted final text")).toBeInTheDocument();
  });
});

describe("professional document detail", () => {
  beforeEach(() => {
    localStorage.setItem("document_analyzer_token", "test-token");
    URL.createObjectURL = vi.fn(() => "blob:secure-preview");
    URL.revokeObjectURL = vi.fn();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it("shows authenticated PDF preview, structured fields, line items and clean missing values", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.endsWith("/auth/me")) return Promise.resolve(response(user));
      if (url.endsWith(`/documents/${id}/file`)) {
        return Promise.resolve(
          new Response(new Blob(["pdf"]), { headers: { "Content-Type": "application/pdf" } }),
        );
      }
      if (url.endsWith(`/documents/${id}`)) {
        return Promise.resolve(
          response({
            ...item("completed"),
            extracted_text: "Invoice Number: INV-42",
            error_details: null,
            invoice_extraction: {
              invoice_number: "INV-42",
              invoice_date: "2026-09-02",
              vendor_name: "Acme",
              customer_name: null,
              currency: "USD",
              subtotal: "10.00",
              tax: "1.00",
              total: "11.00",
              is_valid: true,
              validation_error: null,
              line_items: [
                {
                  position: 0,
                  description: "Consulting",
                  quantity: "1.0000",
                  unit_price: "10.00",
                  line_total: "10.00",
                },
              ],
            },
          }),
        );
      }
      return Promise.resolve(
        response({ items: [item("completed")], total: 1, page: 1, page_size: 100, total_pages: 1 }),
      );
    });

    render(<App />);
    await flush();
    fireEvent.click(screen.getByRole("button", { name: /invoice\.pdf/i }));
    await flush();
    await flush();

    expect(screen.getByTitle("Preview of invoice.pdf")).toHaveAttribute(
      "src",
      "blob:secure-preview",
    );
    expect(screen.getByText("INV-42")).toBeInTheDocument();
    expect(screen.getByText("Acme")).toBeInTheDocument();
    expect(screen.getByText("Not extracted")).toBeInTheDocument();
    expect(screen.getByText("Consulting")).toBeInTheDocument();
    const fileCall = fetchMock.mock.calls.find(([input]) =>
      String(input).endsWith(`/documents/${id}/file`),
    );
    expect(new Headers(fileCall?.[1]?.headers).get("Authorization")).toBe("Bearer test-token");
  });

  it("renders an image preview and a meaningful failed-processing error", async () => {
    const imageItem = {
      ...item("completed"),
      original_filename: "scan.png",
      mime_type: "image/png",
      status: "failed",
    };
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.endsWith("/auth/me")) return Promise.resolve(response(user));
      if (url.endsWith(`/documents/${id}/file`))
        return Promise.resolve(new Response(new Blob(["png"])));
      if (url.endsWith(`/documents/${id}`))
        return Promise.resolve(
          response({
            ...imageItem,
            extracted_text: null,
            error_details: "Image file is corrupt.",
            invoice_extraction: null,
          }),
        );
      return Promise.resolve(
        response({ items: [imageItem], total: 1, page: 1, page_size: 100, total_pages: 1 }),
      );
    });

    render(<App />);
    await flush();
    fireEvent.click(screen.getByRole("button", { name: /scan\.png/i }));
    await flush();
    await flush();

    expect(screen.getByAltText("Preview of scan.png")).toBeInTheDocument();
    expect(screen.getByText("Image file is corrupt.")).toBeInTheDocument();
  });

  it("exports only the supported CSV/XLSX formats with authenticated document context", async () => {
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.endsWith("/auth/me")) return Promise.resolve(response(user));
      if (url.endsWith(`/documents/${id}/file`))
        return Promise.resolve(new Response(new Blob(["pdf"])));
      if (url.endsWith(`/documents/${id}/exports/csv`)) {
        return Promise.resolve(
          new Response(new Blob(["zip"]), {
            headers: {
              "Content-Disposition": 'attachment; filename="invoice-export-20260902.zip"',
            },
          }),
        );
      }
      if (url.endsWith(`/documents/${id}`))
        return Promise.resolve(
          response({
            ...item("completed"),
            extracted_text: "text",
            error_details: null,
            invoice_extraction: null,
          }),
        );
      return Promise.resolve(
        response({ items: [item("completed")], total: 1, page: 1, page_size: 100, total_pages: 1 }),
      );
    });

    render(<App />);
    await flush();
    fireEvent.click(screen.getByRole("button", { name: /invoice\.pdf/i }));
    await flush();
    fireEvent.click(screen.getByRole("button", { name: "Export CSV" }));
    await flush();

    const exportCall = fetchMock.mock.calls.find(([input]) =>
      String(input).endsWith(`/documents/${id}/exports/csv`),
    );
    expect(new Headers(exportCall?.[1]?.headers).get("Authorization")).toBe("Bearer test-token");
    expect(clickSpy).toHaveBeenCalledOnce();
    expect(screen.queryByRole("button", { name: /export pdf/i })).not.toBeInTheDocument();
  });
});

describe("foundation landing page", () => {
  it("renders the product name", () => {
    localStorage.clear();
    render(<App />);
    expect(
      screen.getByRole("img", { name: /invoxa.*document & invoice analyzer/i }),
    ).toBeInTheDocument();
  });
});

describe("commercial authentication interface", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
    window.history.replaceState({}, "", "/");
  });

  it("renders the approved sign-in content and real Google initiation link", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(response({ enabled: true }));
    render(<App />);
    await flush();

    expect(screen.getByRole("heading", { name: "Sign into Invoxa" })).toBeInTheDocument();
    expect(
      screen.getByText(
        "Upload and process business documents, review extracted information with AI, and export structured invoice data.",
      ),
    ).toBeInTheDocument();
    expect(screen.queryByText("Welcome back")).not.toBeInTheDocument();
    expect(screen.queryByText("Secure document workspace")).not.toBeInTheDocument();
    expect(screen.getByText("Don't have an account?")).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Create now" })).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Create account" })).not.toBeInTheDocument();
    expect(screen.getByText("Secure. Private. Built for your business.")).toBeInTheDocument();
    expect(screen.queryByText(/◇/)).not.toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Continue with Google" })).toHaveAttribute(
      "href",
      "http://localhost:8000/auth/google/start",
    );
  });

  it("switches account mode and exposes an accessible password visibility control", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(response({ enabled: false }));
    render(<App />);
    await flush();
    const password = screen.getByLabelText("Password");
    expect(password).toHaveAttribute("type", "password");
    fireEvent.click(screen.getByRole("button", { name: "Show password" }));
    expect(password).toHaveAttribute("type", "text");
    fireEvent.click(screen.getByRole("button", { name: "Create now" }));
    expect(screen.getByRole("heading", { name: "Create your Invoxa account" })).toBeInTheDocument();
    expect(screen.getByText("Already have an account?")).toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Continue with Google" })).not.toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(screen.getByRole("heading", { name: "Sign into Invoxa" })).toBeInTheDocument();
  });

  it("shows safe Google callback errors without provider details", async () => {
    window.history.replaceState({}, "", "/?google_error=invalid_state");
    vi.spyOn(globalThis, "fetch").mockResolvedValue(response({ enabled: true }));
    render(<App />);
    await flush();
    expect(screen.getByRole("alert")).toHaveTextContent(
      "Google sign-in session expired. Please try again.",
    );
    expect(window.location.search).toBe("");
  });

  it("exchanges a one-time Google callback code through the existing token flow", async () => {
    const code = "g".repeat(48);
    window.history.replaceState({}, "", `/?google_code=${code}`);
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.endsWith("/auth/google/config")) return Promise.resolve(response({ enabled: true }));
      if (url.endsWith("/auth/google/exchange")) {
        return Promise.resolve(response({ access_token: "google-invoxa-token" }));
      }
      if (url.endsWith("/auth/me")) return Promise.resolve(response(user));
      return Promise.resolve(
        response({ items: [], total: 0, page: 1, page_size: 100, total_pages: 0 }),
      );
    });
    render(<App />);
    await flush();
    await flush();
    expect(fetchMock).toHaveBeenCalledWith(
      "http://localhost:8000/auth/google/exchange",
      expect.objectContaining({ method: "POST", body: JSON.stringify({ code }) }),
    );
    expect(localStorage.getItem("document_analyzer_token")).toBe("google-invoxa-token");
    expect(window.location.search).toBe("");
  });
});

describe("Phase 9A commercial workspace", () => {
  beforeEach(() => {
    localStorage.setItem("document_analyzer_token", "test-token");
    URL.createObjectURL = vi.fn(() => "blob:report");
    URL.revokeObjectURL = vi.fn();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it("renders compact analytics KPIs, currency-safe values, trends and empty-safe charts", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.endsWith("/auth/me")) return Promise.resolve(response(user));
      if (url.includes("/analytics/summary"))
        return Promise.resolve(
          response({
            kpis: {
              total_documents: 3,
              completed_documents: 2,
              total_invoices: 2,
              currency_totals: [
                { currency: "USD", total: "1250.00", tax: "100.00", average: "625.00" },
              ],
              needs_review: 1,
              ai_findings: 2,
              unresolved_ai_findings: 2,
              high_severity_ai_findings: 1,
              validation_issue_count: 1,
            },
            trends: {
              invoice_spend: [],
              invoice_count: [{ period: "2026-09-02", count: 2 }],
              top_vendors: [{ vendor: "Acme Services", currency: "USD", total: "1250.00" }],
              document_status: { completed: 2 },
              validation_status: { valid: 1, invalid: 1 },
              findings_by_severity: { high: 1, medium: 1 },
              findings_over_time: {},
            },
          }),
        );
      return Promise.resolve(
        response({ items: [], total: 0, page: 1, page_size: 100, total_pages: 0 }),
      );
    });
    render(<App />);
    await flush();
    fireEvent.click(screen.getByRole("button", { name: "Overview" }));
    await flush();
    expect(screen.getByText("Analytics overview")).toBeInTheDocument();
    expect(screen.getAllByText("$1,250.00")).toHaveLength(2);
    expect(screen.getByText("Acme Services")).toBeInTheDocument();
    expect(screen.getByLabelText("2026-09-02: 2 invoices")).toBeInTheDocument();
  });

  it("loads explainable AI findings and supports review lifecycle actions", async () => {
    const finding = {
      id: "finding-1",
      document_id: id,
      document_filename: "invoice.pdf",
      invoice_number: "INV-42",
      vendor_name: "Acme",
      category: "arithmetic_anomaly",
      severity: "high",
      status: "open",
      title: "Invoice total does not reconcile",
      explanation: "Invoice total is USD 1,250, but subtotal plus tax equals USD 1,100.",
      evidence: { subtotal: "1000" },
      affected_fields: ["subtotal", "tax", "total"],
      confidence: null,
      observed_value: "1250.00",
      expected_value: "1100.00",
      created_at: timestamp,
    };
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url.endsWith("/auth/me")) return Promise.resolve(response(user));
      if (url.includes("/ai-analysis/findings") && init?.method === "PATCH")
        return Promise.resolve(response({ ...finding, status: "resolved" }));
      if (url.includes("/ai-analysis/findings"))
        return Promise.resolve(
          response({ items: [finding], total: 1, page: 1, page_size: 50, total_pages: 1 }),
        );
      return Promise.resolve(
        response({ items: [], total: 0, page: 1, page_size: 100, total_pages: 0 }),
      );
    });
    render(<App />);
    await flush();
    fireEvent.click(screen.getByRole("button", { name: "AI Analysis" }));
    await flush();
    expect(screen.getByRole("heading", { level: 1, name: "AI Analysis" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Review findings" })).toBeInTheDocument();
    expect(screen.getAllByRole("heading", { name: "AI Analysis" })).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Invoice total does not reconcile" }));
    expect(screen.getByText(/subtotal plus tax equals/i)).toBeInTheDocument();
    expect(screen.getByText("1250.00")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Resolve" }));
    await flush();
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) =>
          String(url).endsWith("/ai-analysis/findings/finding-1") && init?.method === "PATCH",
      ),
    ).toBe(true);
  });

  it("renders structured reports, keeps raw JSON collapsed, switches type and exports", async () => {
    const clickSpy = vi
      .spyOn(HTMLAnchorElement.prototype, "click")
      .mockImplementation(() => undefined);
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.endsWith("/auth/me")) return Promise.resolve(response(user));
      if (url.includes("/reports/financial/exports/csv"))
        return Promise.resolve(
          new Response("report", {
            headers: { "Content-Disposition": "attachment; filename=invoxa-financial.csv" },
          }),
        );
      if (url.includes("/reports/processing-quality"))
        return Promise.resolve(
          response({
            report_type: "processing-quality",
            summary: {
              total_documents: 8,
              completed_documents: 8,
              needs_review: 8,
              ai_findings: 14,
              currency_totals: [
                { currency: "USD", total: "20943.80", tax: "1550.80", average: "2617.975" },
              ],
            },
            document_status: { completed: 8 },
            validation_status: { valid: 6, needs_review: 2 },
            low_confidence_documents: 2,
          }),
        );
      if (url.includes("/reports/ai-analysis"))
        return Promise.resolve(
          response({
            report_type: "ai-analysis",
            summary: {},
            findings_by_severity: { high: 2 },
            review_status: { open: 14 },
          }),
        );
      if (url.includes("/reports/financial"))
        return Promise.resolve(
          response({
            report_type: "financial",
            summary: {},
            top_vendors: [{ vendor: "Acme Services", currency: "USD", total: "1250.00" }],
          }),
        );
      return Promise.resolve(
        response({ items: [], total: 0, page: 1, page_size: 100, total_pages: 0 }),
      );
    });
    render(<App />);
    await flush();
    fireEvent.click(screen.getByRole("button", { name: "Reports" }));
    fireEvent.change(screen.getByLabelText("Report type"), {
      target: { value: "processing-quality" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Generate report" }));
    await flush();
    expect(screen.getByRole("heading", { name: "Processing Quality Report" })).toBeInTheDocument();
    expect(screen.getByText("$20,943.80")).toBeInTheDocument();
    expect(screen.getByText("$1,550.80")).toBeInTheDocument();
    expect(screen.getByText("$2,617.98")).toBeInTheDocument();
    const rawJson = screen.getByText("View raw JSON").closest("details");
    expect(rawJson).not.toHaveAttribute("open");
    expect(screen.getByRole("button", { name: "Export CSV" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Export XLSX" })).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Export JSON" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Report type"), {
      target: { value: "ai-analysis" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Generate report" }));
    await flush();
    expect(screen.getByRole("heading", { name: "AI Analysis Report" })).toBeInTheDocument();
    fireEvent.change(screen.getByLabelText("Report type"), { target: { value: "financial" } });
    fireEvent.click(screen.getByRole("button", { name: "Generate report" }));
    await flush();
    expect(screen.getByRole("heading", { name: "Financial Summary Report" })).toBeInTheDocument();
    expect(screen.getByText("$1,250.00")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "Export CSV" }));
    await flush();
    expect(clickSpy).toHaveBeenCalledOnce();
  });

  it("uses an accessible custom delete dialog with cancel and Escape without deleting", async () => {
    const confirmSpy = vi.spyOn(window, "confirm");
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      if (String(input).endsWith("/auth/me")) return Promise.resolve(response(user));
      return Promise.resolve(
        response({ items: [item("completed")], total: 1, page: 1, page_size: 100, total_pages: 1 }),
      );
    });
    render(<App />);
    await flush();

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    expect(screen.getByRole("dialog", { name: "Delete document?" })).toHaveTextContent(
      "invoice.pdf",
    );
    expect(screen.getByRole("button", { name: "Cancel" })).toHaveFocus();
    fireEvent.click(screen.getByRole("button", { name: "Cancel" }));
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(fetchMock.mock.calls.some(([, init]) => init?.method === "DELETE")).toBe(false);

    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fireEvent.keyDown(screen.getByRole("dialog"), { key: "Escape" });
    expect(screen.queryByRole("dialog")).not.toBeInTheDocument();
    expect(confirmSpy).not.toHaveBeenCalled();
  });

  it("calls the existing document delete endpoint only after modal confirmation", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input, init) => {
      const url = String(input);
      if (url.endsWith("/auth/me")) return Promise.resolve(response(user));
      if (url.endsWith(`/documents/${id}`) && init?.method === "DELETE")
        return Promise.resolve(new Response(null, { status: 204 }));
      return Promise.resolve(
        response({ items: [item("completed")], total: 1, page: 1, page_size: 100, total_pages: 1 }),
      );
    });
    render(<App />);
    await flush();
    fireEvent.click(screen.getByRole("button", { name: "Delete" }));
    fireEvent.click(screen.getByRole("button", { name: "Delete document" }));
    await flush();
    expect(
      fetchMock.mock.calls.some(
        ([url, init]) => String(url).endsWith(`/documents/${id}`) && init?.method === "DELETE",
      ),
    ).toBe(true);
  });

  it("keeps AI mutation controls hidden for viewers", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.endsWith("/auth/me")) return Promise.resolve(response({ ...user, role: "viewer" }));
      if (url.includes("/ai-analysis/findings"))
        return Promise.resolve(
          response({
            items: [
              {
                id: "f",
                document_id: id,
                document_filename: "invoice.pdf",
                invoice_number: null,
                vendor_name: null,
                category: "missing_information",
                severity: "medium",
                status: "open",
                title: "Invoice number missing",
                explanation: "No invoice number was extracted.",
                evidence: {},
                affected_fields: ["invoice_number"],
                confidence: null,
                observed_value: null,
                expected_value: "Present value",
                created_at: timestamp,
              },
            ],
            total: 1,
            page: 1,
            page_size: 50,
            total_pages: 1,
          }),
        );
      return Promise.resolve(
        response({ items: [], total: 0, page: 1, page_size: 100, total_pages: 0 }),
      );
    });
    render(<App />);
    await flush();
    fireEvent.click(screen.getByRole("button", { name: "AI Analysis" }));
    await flush();
    fireEvent.click(screen.getByRole("button", { name: "Invoice number missing" }));
    expect(screen.queryByRole("button", { name: "Resolve" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Acknowledge" })).not.toBeInTheDocument();
  });
});

describe("frontend authentication security", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it("clears an expired token and protected state without retrying 401 requests", async () => {
    localStorage.setItem("document_analyzer_token", "expired-token");
    const fetchMock = vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(JSON.stringify({ detail: "Not authenticated." }), {
        status: 401,
        headers: { "Content-Type": "application/json" },
      }),
    );

    render(<App />);
    await flush();
    await flush();

    expect(localStorage.getItem("document_analyzer_token")).toBeNull();
    expect(screen.getAllByRole("button", { name: "Sign in" })).toHaveLength(1);
    expect(screen.getByText("Your session expired. Please sign in again.")).toBeInTheDocument();
    expect(fetchMock).toHaveBeenCalledTimes(3);
    await new Promise((resolve) => window.setTimeout(resolve, 20));
    expect(fetchMock).toHaveBeenCalledTimes(3);
  });

  it("hides mutation controls for a viewer role", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      if (String(input).endsWith("/auth/me"))
        return Promise.resolve(response({ ...user, role: "viewer" }));
      return Promise.resolve(
        response({ items: [item("completed")], total: 1, page: 1, page_size: 100, total_pages: 1 }),
      );
    });
    localStorage.setItem("document_analyzer_token", "viewer-token");

    render(<App />);
    await flush();

    expect(screen.queryByText("Upload document")).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Reprocess" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: "Delete" })).not.toBeInTheDocument();
  });
});

describe("password recovery", () => {
  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
    window.history.replaceState({}, "", "/");
  });

  it("opens the forgot-password form and shows the generic success response", async () => {
    vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      if (String(input).endsWith("/auth/google/config")) {
        return Promise.resolve(response({ enabled: false }));
      }
      return Promise.resolve(
        response({
          detail:
            "If an account exists for this email, password reset instructions have been sent.",
        }),
      );
    });
    render(<App />);
    fireEvent.click(screen.getByRole("button", { name: "Forgot password?" }));
    fireEvent.change(screen.getByLabelText("Email"), { target: { value: "person@example.com" } });
    fireEvent.click(screen.getByRole("button", { name: "Send reset instructions" }));
    await flush();

    expect(screen.getByRole("status")).toHaveTextContent("If an account exists");
    expect(globalThis.fetch).toHaveBeenCalledWith(
      expect.stringContaining("/auth/forgot-password"),
      expect.objectContaining({ method: "POST" }),
    );
    fireEvent.click(screen.getByRole("button", { name: "Back to sign in" }));
    expect(screen.getAllByRole("button", { name: "Sign in" })).toHaveLength(1);
  });

  it("validates matching reset passwords without sending the token", async () => {
    window.history.replaceState(
      {},
      "",
      "/reset-password?token=secret-reset-token-value-that-is-long-enough",
    );
    const fetchMock = vi.spyOn(globalThis, "fetch");
    render(<App />);
    fireEvent.change(screen.getByLabelText("New password"), {
      target: { value: "ReplacementPassword123!" },
    });
    fireEvent.change(screen.getByLabelText("Confirm password"), {
      target: { value: "DifferentPassword123!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Reset password" }));

    expect(screen.getByText("Passwords do not match.")).toBeInTheDocument();
    expect(fetchMock).not.toHaveBeenCalled();
    expect(
      screen.queryByText("secret-reset-token-value-that-is-long-enough"),
    ).not.toBeInTheDocument();
  });

  it("handles invalid reset links safely", async () => {
    window.history.replaceState(
      {},
      "",
      "/reset-password?token=secret-reset-token-value-that-is-long-enough",
    );
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      new Response(
        JSON.stringify({ detail: "This password reset link is invalid or has expired." }),
        {
          status: 400,
          headers: { "Content-Type": "application/json" },
        },
      ),
    );
    render(<App />);
    fireEvent.change(screen.getByLabelText("New password"), {
      target: { value: "ReplacementPassword123!" },
    });
    fireEvent.change(screen.getByLabelText("Confirm password"), {
      target: { value: "ReplacementPassword123!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Reset password" }));
    await flush();
    expect(
      screen.getByText("This password reset link is invalid or has expired."),
    ).toBeInTheDocument();
  });

  it("completes reset, removes the token URL, and returns to login", async () => {
    window.history.replaceState(
      {},
      "",
      "/reset-password?token=secret-reset-token-value-that-is-long-enough",
    );
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      response({ detail: "Your password has been reset. You can now sign in." }),
    );
    render(<App />);
    fireEvent.change(screen.getByLabelText("New password"), {
      target: { value: "ReplacementPassword123!" },
    });
    fireEvent.change(screen.getByLabelText("Confirm password"), {
      target: { value: "ReplacementPassword123!" },
    });
    fireEvent.click(screen.getByRole("button", { name: "Reset password" }));
    await flush();

    expect(screen.getByRole("status")).toHaveTextContent("Your password has been reset");
    expect(window.location.search).toBe("");
    fireEvent.click(screen.getByRole("button", { name: "Back to sign in" }));
    expect(screen.getAllByRole("button", { name: "Sign in" })).toHaveLength(1);
  });
});

describe("export and productivity controls", () => {
  beforeEach(() => {
    localStorage.setItem("document_analyzer_token", "test-token");
    URL.createObjectURL = vi.fn(() => "blob:export");
    URL.revokeObjectURL = vi.fn();
  });

  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it("combines search, filters, sorting, and pagination in server requests", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      if (String(input).endsWith("/auth/me")) return Promise.resolve(response(user));
      return Promise.resolve(
        response({ items: [item("completed")], total: 21, page: 1, page_size: 20, total_pages: 2 }),
      );
    });
    render(<App />);
    await flush();
    fireEvent.change(screen.getByLabelText("Search documents"), { target: { value: "Acme" } });
    fireEvent.change(screen.getByLabelText("Status"), { target: { value: "completed" } });
    fireEvent.change(screen.getByLabelText("File type"), { target: { value: "application/pdf" } });
    fireEvent.change(screen.getByLabelText("Sort"), { target: { value: "filename:asc" } });
    expect(screen.queryByRole("button", { name: "Search" })).not.toBeInTheDocument();
    const combinedUrl = await waitFor(() => {
      const url = fetchMock.mock.calls
        .map(([input]) => String(input))
        .find((requestUrl) => requestUrl.includes("search=Acme"));
      expect(url).toBeDefined();
      return url;
    });
    expect(combinedUrl).toContain("status=completed");
    expect(combinedUrl).toContain("mime_type=application%2Fpdf");
    expect(combinedUrl).toContain("sort_by=filename");
    expect(combinedUrl).toContain("sort_direction=asc");
    fireEvent.click(screen.getByRole("button", { name: "Next" }));
    await flush();
    expect(fetchMock.mock.calls.some(([input]) => String(input).includes("page=2"))).toBe(true);
  });

  it("downloads selected documents in bulk and offers single JSON export", async () => {
    vi.spyOn(HTMLAnchorElement.prototype, "click").mockImplementation(() => undefined);
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((input) => {
      const url = String(input);
      if (url.endsWith("/auth/me")) return Promise.resolve(response(user));
      if (url.endsWith(`/documents/${id}/file`))
        return Promise.resolve(new Response(new Blob(["pdf"])));
      if (url.endsWith(`/documents/${id}`))
        return Promise.resolve(
          response({
            ...item("completed"),
            extracted_text: "text",
            error_details: null,
            invoice_extraction: null,
          }),
        );
      if (url.includes("/exports/json"))
        return Promise.resolve(
          new Response(JSON.stringify([]), {
            headers: {
              "Content-Type": "application/json",
              "Content-Disposition": 'attachment; filename="invoice-export.json"',
            },
          }),
        );
      return Promise.resolve(
        response({ items: [item("completed")], total: 1, page: 1, page_size: 20, total_pages: 1 }),
      );
    });
    render(<App />);
    await flush();
    fireEvent.click(screen.getByLabelText("Select invoice.pdf"));
    expect(screen.getByText("1 selected")).toBeInTheDocument();
    fireEvent.click(screen.getByRole("button", { name: "JSON" }));
    await flush();
    const bulkCall = fetchMock.mock.calls.find(([input]) =>
      String(input).endsWith("/exports/json"),
    );
    expect(bulkCall?.[1]?.method).toBe("POST");
    expect(String(bulkCall?.[1]?.body)).toContain(id);

    fireEvent.click(screen.getByRole("button", { name: /invoice\.pdf/i }));
    await flush();
    expect(screen.getByRole("button", { name: "Export JSON" })).toBeInTheDocument();
  });
});
