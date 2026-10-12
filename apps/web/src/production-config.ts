function validateTrustedHttpsUrl(value: string, name: string): void {
  try {
    type UrlParts = {
      protocol: string;
      hostname: string;
      username: string;
      password: string;
      search: string;
      hash: string;
    };
    const Url = (globalThis as unknown as { URL: new (url: string) => UrlParts }).URL;
    const parsed = new Url(value);
    const host = parsed.hostname.toLowerCase().replace(/^\[|\]$/g, "");
    const loopbackV4 = /^127(?:\.\d{1,3}){3}$/.test(host);
    if (
      parsed.protocol !== "https:" ||
      !parsed.hostname ||
      parsed.username ||
      parsed.password ||
      parsed.search ||
      parsed.hash ||
      /[\s\\]/.test(value) ||
      host === "localhost" ||
      host.endsWith(".localhost") ||
      host === "::1" ||
      host === "::" ||
      host === "0.0.0.0" ||
      loopbackV4
    ) {
      throw new Error();
    }
  } catch {
    throw new Error(`Production ${name} must be a trusted HTTPS URL.`);
  }
}

export function validateProductionApiUrl(value: string | undefined): void {
  if (!value) {
    throw new Error("Production builds require VITE_API_BASE_URL with an HTTPS API URL.");
  }
  validateTrustedHttpsUrl(value, "VITE_API_BASE_URL");
}

export function validateProductionAppUrl(value: string | undefined): void {
  if (value) validateTrustedHttpsUrl(value, "VITE_PUBLIC_APP_URL");
}
