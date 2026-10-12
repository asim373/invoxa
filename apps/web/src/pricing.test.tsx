import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";

import { PricingPage } from "./pricing";

describe("public pricing page", () => {
  it("shows approved plans, allowances and honest unavailable purchase actions", () => {
    render(<PricingPage />);

    expect(screen.getByRole("heading", { name: "Standard" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Professional" })).toBeInTheDocument();
    expect(screen.getByRole("heading", { name: "Enterprise" })).toBeInTheDocument();
    expect(screen.getByText("$99")).toBeInTheDocument();
    expect(screen.getByText("1,000 pages / month")).toBeInTheDocument();
    expect(screen.getAllByText("2 GB").length).toBeGreaterThan(0);
    expect(screen.getByText("1 active seat")).toBeInTheDocument();
    expect(screen.getByText("$249")).toBeInTheDocument();
    expect(screen.getByText("5,000 pages / month")).toBeInTheDocument();
    expect(screen.getAllByText("10 GB").length).toBeGreaterThan(0);
    expect(screen.getByText("5 active seats")).toBeInTheDocument();
    expect(screen.getByText(/Everything in Standard, plus/)).toBeInTheDocument();
    expect(screen.getByText(/Everything in Professional, plus/)).toBeInTheDocument();
    expect(screen.getByText("Custom Pricing")).toBeInTheDocument();
    expect(screen.getByText("Contact Sales")).toBeInTheDocument();
    expect(screen.getByText("5,001+ pages / month")).toBeInTheDocument();
    expect(screen.getAllByText("11 GB+").length).toBeGreaterThan(0);
    expect(screen.getByText("6+ active seats")).toBeInTheDocument();
    expect(screen.getByRole("link", { name: "Create a workspace" })).toHaveAttribute(
      "href",
      "/?signup=1",
    );
    expect(screen.getByRole("link", { name: "Create account on Standard" })).toHaveAttribute(
      "href",
      "/?signup=1",
    );
    expect(screen.getByText(/Professional plan selection and billing are not available yet/)).toBeInTheDocument();
    expect(screen.getByRole("button", { name: "Contact sales unavailable" })).toBeDisabled();
    expect(screen.getByText(/API upload, individual result retrieval and export are not available/)).toBeInTheDocument();
  });

  it("toggles approved monthly and annual pricing accessibly", () => {
    render(<PricingPage />);

    const annual = screen.getByRole("button", { name: "Annual" });
    fireEvent.click(annual);
    expect(annual).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("$990")).toBeInTheDocument();
    expect(screen.getByText("$2,490")).toBeInTheDocument();
    expect(screen.getByText("Save $198/year")).toBeInTheDocument();
    expect(screen.getByText("Save $498/year")).toBeInTheDocument();

    const monthly = screen.getByRole("button", { name: "Monthly" });
    fireEvent.click(monthly);
    expect(monthly).toHaveAttribute("aria-pressed", "true");
    expect(screen.getByText("$99")).toBeInTheDocument();
    expect(screen.getByText("$249")).toBeInTheDocument();
    expect(screen.queryByText(/Billed monthly|Billed annually/)).not.toBeInTheDocument();
  });

  it("provides a semantic plan comparison table without internal pricing floors", () => {
    render(<PricingPage />);

    expect(screen.getByRole("table")).toBeInTheDocument();
    expect(screen.getByText("Document management & search")).toBeInTheDocument();
    expect(screen.getByText("SAML SSO")).toBeInTheDocument();
    expect(screen.getByText("SCIM user provisioning")).toBeInTheDocument();
    expect(screen.getByText("Dedicated priority support & SLA")).toBeInTheDocument();
    expect(screen.getByText("On-premises / private cloud deployment")).toBeInTheDocument();
    expect(screen.getByText("Custom integrations")).toBeInTheDocument();
    expect(screen.getByText("Read-only document-list API")).toBeInTheDocument();
    expect(screen.getByText("Enterprise processing limits, storage, and team capacity are tailored to your organization's requirements.")).toBeInTheDocument();
    expect(screen.queryByText("Monthly pages", { selector: "th" })).not.toBeInTheDocument();
    expect(screen.queryByText("Source storage", { selector: "th" })).not.toBeInTheDocument();
    expect(screen.queryByText("Active seats", { selector: "th" })).not.toBeInTheDocument();
    expect(screen.queryByText(/799|7,990/)).not.toBeInTheDocument();
  });
});
