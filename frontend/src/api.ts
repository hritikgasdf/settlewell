// One fetch client for the local demo API. Mutations are never retried automatically.
// The CSRF token is kept in memory for this page only; it is never persisted.
import type {
  IncidentDetail,
  IncidentList,
  ProposalActionResponse,
  RejectResponse,
  Session,
} from './types';

export class ApiError extends Error {
  readonly status: number;
  readonly code: string;
  readonly gateCode: string | null;
  readonly intentTerminal: boolean;
  /** True when a POST may have changed state but the result is not known. The caller must not retry. */
  readonly outcomeUnknown: boolean;

  constructor(init: {
    status: number;
    code: string;
    message: string;
    gateCode?: string | null;
    intentTerminal?: boolean;
    outcomeUnknown: boolean;
  }) {
    super(init.message);
    this.name = 'ApiError';
    this.status = init.status;
    this.code = init.code;
    this.gateCode = init.gateCode ?? null;
    this.intentTerminal = init.intentTerminal ?? false;
    this.outcomeUnknown = init.outcomeUnknown;
  }
}

export function describeError(e: unknown): string {
  if (e instanceof ApiError) {
    const gate = e.gateCode ? `; gate code ${e.gateCode}` : '';
    return `${e.message} [${e.code}${gate}]`;
  }
  return e instanceof Error ? e.message : 'Unexpected error';
}

const CSRF_HEADER = 'X-Settlewell-CSRF';
let csrfToken: string | null = null;

function errorBody(data: unknown): Record<string, unknown> | null {
  if (data !== null && typeof data === 'object' && 'error' in data) {
    const err = (data as { error: unknown }).error;
    if (err !== null && typeof err === 'object') return err as Record<string, unknown>;
  }
  return null;
}

async function request<T>(method: 'GET' | 'POST', path: string, body?: unknown, csrf?: string): Promise<T> {
  const headers: Record<string, string> = { Accept: 'application/json' };
  if (body !== undefined) headers['Content-Type'] = 'application/json';
  if (csrf) headers[CSRF_HEADER] = csrf;
  const isPost = method === 'POST';

  let res: Response;
  try {
    res = await fetch(path, {
      method,
      headers,
      body: body === undefined ? undefined : JSON.stringify(body),
      credentials: 'same-origin',
      cache: 'no-store',
    });
  } catch {
    throw new ApiError({
      status: 0,
      code: 'network_error',
      message: isPost
        ? 'No response from the local API. The action may or may not have been applied. Nothing was retried.'
        : 'Cannot reach the local API. Start it with `uv run python -m backend.api`, then reload.',
      outcomeUnknown: isPost,
    });
  }

  let data: unknown = null;
  try {
    data = await res.json();
  } catch {
    data = null;
  }
  if (res.ok) return data as T;

  const err = errorBody(data);
  const rawCode = err?.code;
  const rawMessage = err?.message;
  const rawGate = err?.gate_code;
  const code = typeof rawCode === 'string' ? rawCode : `http_${res.status}`;
  const message = typeof rawMessage === 'string' ? rawMessage : `Request failed with HTTP ${res.status}.`;
  // 503 writes_disabled and db_missing are refused before any write, so they are not unknown.
  const refusedBeforeWrite = code === 'writes_disabled' || code === 'db_missing';
  const outcomeUnknown =
    isPost && ((res.status >= 500 && !refusedBeforeWrite) || code === 'in_flight' || code === 'remote_unknown');
  throw new ApiError({
    status: res.status,
    code,
    message,
    gateCode: typeof rawGate === 'string' ? rawGate : null,
    intentTerminal: err?.intent_terminal === true,
    outcomeUnknown,
  });
}

/** Fetches a fresh per-page CSRF token. Call on load and after csrf_invalid. */
export async function loadSession(): Promise<Session> {
  const s = await request<Session>('GET', '/api/session');
  csrfToken = s.csrf_token;
  return s;
}

async function post<T>(path: string, body: unknown): Promise<T> {
  if (csrfToken === null) {
    await loadSession();
  }
  try {
    return await request<T>('POST', path, body, csrfToken ?? undefined);
  } catch (e) {
    // The server rejected the request before doing anything. Refresh the token for the next click.
    // No automatic re-send.
    if (e instanceof ApiError && (e.code === 'csrf_invalid' || e.code === 'csrf_missing')) {
      await loadSession().catch(() => undefined);
    }
    throw e;
  }
}

export function listIncidents(): Promise<IncidentList> {
  return request<IncidentList>('GET', '/api/incidents');
}

export function getIncident(id: number): Promise<IncidentDetail> {
  return request<IncidentDetail>('GET', `/api/incidents/${id}`);
}

/** Only the binding hash goes up. The server takes terms, actor and approval id from its own record. */
export function approveProposal(incidentId: number, intentId: number, bindingHash: string): Promise<ProposalActionResponse> {
  return post<ProposalActionResponse>(`/api/incidents/${incidentId}/proposals/${intentId}/approve`, {
    binding_hash: bindingHash,
  });
}

export function rejectProposal(
  incidentId: number,
  intentId: number,
  bindingHash: string,
  reason: string,
): Promise<RejectResponse> {
  return post<RejectResponse>(`/api/incidents/${incidentId}/proposals/${intentId}/reject`, {
    binding_hash: bindingHash,
    reason,
  });
}

/** Sends expected_status SCHEDULED only. The server derives the terms from the stored transfer. */
export function cancelTransfer(incidentId: number, transferId: number): Promise<ProposalActionResponse> {
  return post<ProposalActionResponse>(`/api/incidents/${incidentId}/transfers/${transferId}/cancel`, {
    expected_status: 'SCHEDULED',
  });
}
