import { fireEvent, render, screen } from "@testing-library/react";
import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";

import { OrganizationSettings } from "./organization-settings";

function response(body: unknown, status = 200): Response {
  return new Response(JSON.stringify(body), {
    status,
    headers: { "Content-Type": "application/json" },
  });
}

describe("company profile settings", () => {
  beforeEach(() => {
    localStorage.setItem("document_analyzer_token", "test-token");
    localStorage.setItem("document_analyzer_organization_id", "org-1");
  });

  afterEach(() => {
    vi.restoreAllMocks();
    localStorage.clear();
  });

  it("loads a tenant profile and saves the display name without changing its slug", async () => {
    const fetchMock = vi.spyOn(globalThis, "fetch").mockImplementation((_, init) => {
      if ((init?.method ?? "GET") === "PATCH") {
        return Promise.resolve(response({ id: "org-1", name: "Northwind Studio", slug: "northwind" }));
      }
      return Promise.resolve(response({ id: "org-1", name: "Northwind", slug: "northwind" }));
    });

    render(<OrganizationSettings canEdit />);
    const name = await screen.findByLabelText("Company or workspace name");
    expect(screen.getByLabelText("Workspace sign-in identifier")).toHaveValue("northwind");
    fireEvent.change(name, { target: { value: "  Northwind Studio  " } });
    fireEvent.click(screen.getByRole("button", { name: "Save company profile" }));

    expect(await screen.findByRole("status")).toHaveTextContent("Company profile saved.");
    expect(fetchMock.mock.calls.some(([url, init]) => String(url).endsWith("/organizations/current") && init?.method === "PATCH")).toBe(true);
    expect(screen.getByLabelText("Workspace sign-in identifier")).toHaveValue("northwind");
  });

  it("keeps the profile read-only for non-admin members", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      response({ id: "org-1", name: "Northwind", slug: "northwind" }),
    );
    render(<OrganizationSettings canEdit={false} />);
    await screen.findByLabelText("Company or workspace name");
    expect(screen.getByLabelText("Company or workspace name")).toHaveAttribute("readonly");
    expect(screen.queryByRole("button", { name: "Save company profile" })).not.toBeInTheDocument();
    expect(screen.getByText("Only workspace owners and admins can edit company details.")).toBeInTheDocument();
  });

  it("shows load failures accessibly", async () => {
    vi.spyOn(globalThis, "fetch").mockResolvedValue(
      response({ detail: "Workspace unavailable." }, 403),
    );
    render(<OrganizationSettings canEdit />);
    expect(await screen.findByRole("alert")).toHaveTextContent("Workspace unavailable.");
  });
});
