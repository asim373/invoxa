import { StrictMode } from "react";
import { cleanup, fireEvent, render, screen, waitFor } from "@testing-library/react";
import { afterEach, beforeEach, expect, it, vi } from "vitest";

import { App } from "./main";

beforeEach(() => {
  localStorage.clear();
  window.history.replaceState({}, "", "/verify-email?token=disposable-token");
});

afterEach(() => {
  cleanup();
  vi.restoreAllMocks();
  window.history.replaceState({}, "", "/");
});

it("keeps verification usable in StrictMode and removes the token from the address bar", async () => {
  const fetcher = vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
    JSON.stringify({ detail: "Email verified. You can now sign in." }),
    { headers: { "Content-Type": "application/json" } },
  ));
  render(<StrictMode><App /></StrictMode>);
  expect(window.location.search).toBe("");
  fireEvent.click(screen.getByRole("button", { name: /^Verify email$/ }));
  await waitFor(() => expect(screen.getByText("Email verified. You can now sign in.")).toBeVisible());
  expect(fetcher.mock.calls.some(([url, request]) =>
    String(url).endsWith("/auth/verify-email") &&
    JSON.parse(String(request?.body)).token === "disposable-token",
  )).toBe(true);
});

it("shows invalid or expired link errors and offers resend", async () => {
  vi.spyOn(globalThis, "fetch").mockResolvedValue(new Response(
    JSON.stringify({ detail: "This verification link is invalid or has expired." }),
    { status: 400, headers: { "Content-Type": "application/json" } },
  ));
  render(<App />);
  fireEvent.click(screen.getByRole("button", { name: /^Verify email$/ }));
  await waitFor(() => expect(screen.getByRole("alert")).toHaveTextContent("expired"));
  expect(screen.getByRole("button", { name: "Resend verification email" })).toBeEnabled();
  expect(screen.getByLabelText("Email")).toBeVisible();
});

it("offers an accessible resend flow from login", async () => {
  window.history.replaceState({}, "", "/");
  vi.spyOn(globalThis, "fetch").mockImplementation(async () => new Response(
    JSON.stringify({ detail: "If verification is needed, a verification email will be sent." }),
    { headers: { "Content-Type": "application/json" } },
  ));
  render(<App />);
  fireEvent.click(screen.getByRole("button", { name: "Verify email or resend verification" }));
  fireEvent.change(screen.getByLabelText("Email"), { target: { value: "pending@example.com" } });
  fireEvent.click(screen.getByRole("button", { name: "Resend verification email" }));
  await waitFor(() => expect(screen.getByRole("status")).toHaveTextContent("verification email"));
});
