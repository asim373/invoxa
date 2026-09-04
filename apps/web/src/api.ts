export const API_BASE_URL = (import.meta.env.VITE_API_BASE_URL ?? "http://localhost:8000").replace(
  /\/$/,
  "",
);
export const AUTHENTICATION_LOST_EVENT = "document-analyzer:authentication-lost";

export class AuthenticationLostError extends Error {
  constructor() {
    super("Your session expired. Please sign in again.");
  }
}

export function getStoredToken(): string | null {
  return localStorage.getItem("document_analyzer_token");
}

export function setStoredToken(token: string): void {
  localStorage.setItem("document_analyzer_token", token);
}

export function clearStoredToken(): void {
  localStorage.removeItem("document_analyzer_token");
}

function handleUnauthorizedResponse(response: Response, authenticatedRequest: boolean): boolean {
  if (response.status !== 401 || !authenticatedRequest) return false;
  if (getStoredToken()) {
    clearStoredToken();
    window.dispatchEvent(new Event(AUTHENTICATION_LOST_EVENT));
  }
  return true;
}

export async function authenticatedFetch(
  path: string,
  options: RequestInit = {},
): Promise<Response> {
  const headers = new Headers(options.headers);
  const token = getStoredToken();
  if (token) headers.set("Authorization", `Bearer ${token}`);
  const response = await fetch(`${API_BASE_URL}${path}`, { ...options, headers });
  if (handleUnauthorizedResponse(response, Boolean(token))) throw new AuthenticationLostError();
  return response;
}

export async function apiRequest<T>(path: string, options: RequestInit = {}): Promise<T> {
  const token = getStoredToken();
  const headers = new Headers(options.headers);

  if (!(options.body instanceof FormData)) {
    headers.set("Content-Type", "application/json");
  }

  if (token) {
    headers.set("Authorization", `Bearer ${token}`);
  }

  const response = await fetch(`${API_BASE_URL}${path}`, {
    ...options,
    headers,
  });
  if (handleUnauthorizedResponse(response, Boolean(token))) throw new AuthenticationLostError();

  if (!response.ok) {
    let message = `Request failed with status ${response.status}.`;

    try {
      const data = await response.json();

      if (typeof data.detail === "string") {
        message = data.detail;
      } else if (Array.isArray(data.detail)) {
        message = data.detail
          .map((item: { msg?: string }) => item.msg ?? "Invalid request.")
          .join(", ");
      }
    } catch {
      // Keep fallback message.
    }

    throw new Error(message);
  }

  if (response.status === 204) {
    return undefined as T;
  }

  const contentType = response.headers.get("content-type") ?? "";

  if (!contentType.includes("application/json")) {
    return undefined as T;
  }

  return response.json() as Promise<T>;
}
