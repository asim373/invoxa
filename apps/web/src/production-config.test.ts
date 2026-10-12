import { describe, expect, it } from "vitest";

import { validateProductionApiUrl, validateProductionAppUrl } from "./production-config";

describe("production frontend API configuration", () => {
  it("accepts an explicit HTTPS API base including a reverse-proxy path", () => {
    expect(() => validateProductionApiUrl("https://api.example.com/v1")).not.toThrow();
  });

  it.each([
    undefined,
    "",
    "http://api.example.com",
    "https://localhost:8000",
    "https://tenant.localhost",
    "https://127.0.0.1:8000",
    "https://[::1]:8000",
    "https://api.example.com?target=localhost",
  ])("rejects unsafe production API URL %s", (value) => {
    expect(() => validateProductionApiUrl(value)).toThrow(/trusted HTTPS URL|require VITE_API/);
  });

  it("allows an omitted app URL to use the browser origin and rejects unsafe overrides", () => {
    expect(() => validateProductionAppUrl(undefined)).not.toThrow();
    expect(() => validateProductionAppUrl("https://app.example.com")).not.toThrow();
    expect(() => validateProductionAppUrl("http://localhost:5173")).toThrow(
      "Production VITE_PUBLIC_APP_URL must be a trusted HTTPS URL.",
    );
  });
});
