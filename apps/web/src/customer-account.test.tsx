import React from "react";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { AccountRecovery, CustomerAccount, EmailChangeConfirmation } from "./customer-account";
import { apiRequest } from "./api";

vi.mock("./api", () => ({ apiRequest: vi.fn(), clearStoredToken: vi.fn() }));
const api = vi.mocked(apiRequest);

describe("customer account", () => {
  beforeEach(() => { vi.clearAllMocks(); window.history.replaceState({}, "", "/"); });
  it("loads and saves only allowed profile fields", async () => {
    api.mockResolvedValueOnce({ display_name: "Demo", email: "demo@example.com", identity_managed: false, email_verified: true });
    api.mockResolvedValueOnce({display_name: "Updated"});
    render(<CustomerAccount />);
    const input = await screen.findByLabelText("Display name");
    fireEvent.change(input, {target: {value: "Updated"}});
    fireEvent.click(screen.getByRole("button", {name: "Save profile"}));
    await screen.findByText("Profile saved.");
    expect(api).toHaveBeenLastCalledWith("/auth/account/profile", expect.objectContaining({body: JSON.stringify({display_name: "Updated"})}));
  });
  it("protects managed identity while keeping profile editable", async () => {
    api.mockResolvedValueOnce({ display_name: "Demo", email: "demo@example.com", identity_managed: true, email_verified: true });
    render(<CustomerAccount />);
    await screen.findByLabelText("Display name");
    expect(screen.queryByLabelText("New email")).toBeNull();
    expect(screen.getByText(/managed by your identity provider/)).toBeTruthy();
  });
  it("confirmation strips token from URL and requires an explicit action", async () => {
    window.history.replaceState({}, "", "/confirm-email-change?token=recipient-only-test-token");
    api.mockResolvedValueOnce({detail: "Login email changed."});
    render(<React.StrictMode><EmailChangeConfirmation /></React.StrictMode>);
    expect(window.location.search).toBe("");
    expect(api).not.toHaveBeenCalled();
    fireEvent.click(screen.getByRole("button", {name: "Confirm email change"}));
    await waitFor(() => expect(screen.getByRole("status").textContent).toContain("Login email changed"));
  });
  it("shows missing or invalid link safely", () => {
    render(<EmailChangeConfirmation />);
    expect(screen.getByRole("alert").textContent).toContain("invalid or has expired");
    expect(screen.getByRole("button")).toBeDisabled();
  });
  it("requires explicit permanent-deletion confirmation", async () => {
    api.mockResolvedValueOnce({display_name: "Demo", email: "demo@example.com", identity_managed: false, email_verified: true});
    render(<CustomerAccount />);
    expect(await screen.findByRole("button", {name: "Delete Account Permanently"})).toBeDisabled();
    fireEvent.change(screen.getByLabelText("Type DELETE"), {target: {value: "DELETE"}});
    expect(screen.getByRole("button", {name: "Delete Account Permanently"})).not.toBeDisabled();
    expect(screen.getByRole("button", {name: "Deactivate account"})).toBeDisabled();
  });
  it("clears the session and displays an irreversible deletion outcome", async () => {
    const { clearStoredToken } = await import("./api");
    api.mockResolvedValueOnce({display_name: "Demo", email: "demo@example.com", identity_managed: false, email_verified: true});
    api.mockResolvedValueOnce({detail: "Account permanently deleted."});
    render(<CustomerAccount />);
    await screen.findByLabelText("Type DELETE");
    fireEvent.change(screen.getAllByLabelText("Current password").at(-1)!, {target: {value: "Disposable-passphrase-123"}});
    fireEvent.change(screen.getByLabelText("Type DELETE"), {target: {value: "DELETE"}});
    fireEvent.click(screen.getByRole("button", {name: "Delete Account Permanently"}));
    await screen.findByRole("status");
    expect(clearStoredToken).toHaveBeenCalled();
    expect(api).toHaveBeenLastCalledWith("/auth/account/delete", expect.objectContaining({body: JSON.stringify({current_password: "Disposable-passphrase-123", confirmation: "DELETE"})}));
  });
  it("recovery requires password and explicit submission", async () => {
    window.history.replaceState({}, "", "/reactivate-account?token=recipient-only-test-token");
    api.mockResolvedValueOnce({detail: "Account reactivated. Sign in again."});
    render(<AccountRecovery />);
    expect(window.location.search).toBe("");
    expect(api).not.toHaveBeenCalled();
    fireEvent.change(screen.getByLabelText("Current password"), {target: {value: "Disposable-password-only"}});
    fireEvent.click(screen.getByRole("button", {name: "Confirm reactivation"}));
    await screen.findByText("Account reactivated. Sign in again.");
  });
});
