const API_BASE = process.env.NEXT_PUBLIC_API_URL || "";

async function apiFetch<T>(path: string, init?: RequestInit): Promise<T> {
  const url = `${API_BASE}${path}`;
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    ...(init?.headers as Record<string, string>),
  };
  const apiKey = process.env.NEXT_PUBLIC_API_KEY;
  if (apiKey) {
    headers["Authorization"] = `Bearer ${apiKey}`;
  }
  const res = await fetch(url, { ...init, headers });
  if (!res.ok) {
    throw new Error(`API error ${res.status}: ${await res.text()}`);
  }
  return res.json();
}

/* ── Runs ── */

export interface Run {
  id: string;
  title: string;
  goal: string;
  status: string;
  phase: string;
  current_iteration: number;
  max_iterations: number;
  event_count: number;
  created_at: string;
  updated_at: string;
  last_error: string | null;
}

export interface RunDetail extends Run {
  manifest: Record<string, unknown> | null;
  metadata: Record<string, unknown>;
}

export function listRuns(): Promise<{ runs: Run[] }> {
  return apiFetch("/api/research/runs");
}

export function getRun(id: string): Promise<{ run: RunDetail }> {
  return apiFetch(`/api/research/runs/${id}`);
}

export function createRun(body: {
  name: string;
  goal?: string;
  manifest?: Record<string, unknown>;
  max_iterations?: number;
}): Promise<{ run: Run }> {
  return apiFetch("/api/research/runs", {
    method: "POST",
    body: JSON.stringify(body),
  });
}

export function controlRun(
  id: string,
  action: "pause" | "resume" | "stop"
): Promise<{ run: Run }> {
  return apiFetch(`/api/research/runs/${id}/${action}`, { method: "POST" });
}

/* ── Candidates ── */

export interface Candidate {
  id: string;
  iteration: number;
  title: string;
  summary: string;
  status: string;
  created_at: string;
  metadata: Record<string, unknown>;
}

export function listCandidates(
  runId: string
): Promise<{ candidates: Candidate[] }> {
  return apiFetch(`/api/research/runs/${runId}/candidates`);
}

/* ── Metrics ── */

export interface Metric {
  id: string;
  iteration: number;
  name: string;
  value: number;
  created_at: string;
}

export function listMetrics(runId: string): Promise<{ metrics: Metric[] }> {
  return apiFetch(`/api/research/runs/${runId}/metrics`);
}

/* ── Events ── */

export interface ResearchEvent {
  id: string;
  sequence: number;
  event_type: string;
  timestamp: string;
  payload: Record<string, unknown>;
}

export function listEvents(
  runId: string,
  after?: number
): Promise<{ events: ResearchEvent[] }> {
  const qs = after ? `?after=${after}` : "";
  return apiFetch(`/api/research/runs/${runId}/events${qs}`);
}

/* ── Reports ── */

export interface Report {
  id: string;
  kind: string;
  title: string;
  content: string;
  created_at: string;
}

export function listReports(runId: string): Promise<{ reports: Report[] }> {
  return apiFetch(`/api/research/runs/${runId}/reports`);
}

/* ── Chat ── */

export interface ChatMessage {
  id: string;
  content: string;
  scope: string;
  timestamp: string;
  metadata: Record<string, unknown>;
}

export function sendRunChat(
  runId: string,
  message: string,
  author = "operator"
): Promise<{ data: Record<string, unknown> }> {
  return apiFetch(`/api/research/runs/${runId}/chat`, {
    method: "POST",
    body: JSON.stringify({ message, author }),
  });
}

export function sendGlobalChat(
  message: string,
  author = "operator"
): Promise<{ data: Record<string, unknown> }> {
  return apiFetch("/api/research/chat", {
    method: "POST",
    body: JSON.stringify({ message, author }),
  });
}

export function listGlobalChat(): Promise<{ messages: ChatMessage[] }> {
  return apiFetch("/api/research/chat");
}

export function requestMutation(
  runId: string,
  reason = ""
): Promise<Record<string, unknown>> {
  return apiFetch(`/api/research/runs/${runId}/request-mutation`, {
    method: "POST",
    body: JSON.stringify({ reason }),
  });
}

/* ── Interactive Chat (via /v1/chat/completions) ── */

export interface CompletionMessage {
  role: "system" | "user" | "assistant";
  content: string;
}

/**
 * Send a conversation to Hermes and stream the response token-by-token.
 * Calls the OpenAI-compatible /v1/chat/completions endpoint with stream=true.
 * Returns the full assistant message once the stream is done.
 */
export async function chatWithHermes(
  messages: CompletionMessage[],
  onDelta: (token: string) => void,
  signal?: AbortSignal,
): Promise<string> {
  const url = `${API_BASE}/v1/chat/completions`;
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
  };
  const apiKey = process.env.NEXT_PUBLIC_API_KEY;
  if (apiKey) {
    headers["Authorization"] = `Bearer ${apiKey}`;
  }

  const res = await fetch(url, {
    method: "POST",
    headers,
    body: JSON.stringify({
      model: "hermes-agent",
      messages,
      stream: true,
    }),
    signal,
  });

  if (!res.ok) {
    throw new Error(`API error ${res.status}: ${await res.text()}`);
  }

  const reader = res.body?.getReader();
  if (!reader) throw new Error("No response body");

  const decoder = new TextDecoder();
  let full = "";
  let buffer = "";

  while (true) {
    const { done, value } = await reader.read();
    if (done) break;

    buffer += decoder.decode(value, { stream: true });
    const lines = buffer.split("\n");
    buffer = lines.pop() ?? "";

    for (const line of lines) {
      const trimmed = line.trim();
      if (!trimmed || !trimmed.startsWith("data: ")) continue;
      const payload = trimmed.slice(6);
      if (payload === "[DONE]") continue;

      try {
        const parsed = JSON.parse(payload);
        const delta = parsed.choices?.[0]?.delta?.content;
        if (delta) {
          full += delta;
          onDelta(delta);
        }
      } catch {
        // skip malformed chunks
      }
    }
  }

  return full;
}

/* ── Iterations / Mutation Audit ── */

export function listIterations(
  runId: string
): Promise<{ iterations: Record<string, unknown>[] }> {
  return apiFetch(`/api/research/runs/${runId}/iterations`);
}

export function getMutationAudit(
  runId: string,
  iteration: number
): Promise<Record<string, unknown>> {
  return apiFetch(
    `/api/research/runs/${runId}/iterations/${iteration}/mutation-audit`
  );
}
