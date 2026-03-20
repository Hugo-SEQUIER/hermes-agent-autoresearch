"use client";

import React, { useState, useEffect, useCallback } from "react";
import { Shell } from "@/components/Shell";
import { Icon } from "@/components/Icon";
import ChatPanel from "@/components/ChatPanel";
import MetricsChart from "@/components/MetricsChart";
import {
  getRun,
  listCandidates,
  listMetrics,
  listEvents,
  controlRun,
  requestMutation,
  RunDetail,
  Candidate,
  Metric,
  ResearchEvent,
} from "@/lib/api";
import { useRunSSE, SSEEvent } from "@/lib/useSSE";

/* ── Style maps ── */

const statusStyles: Record<string, string> = {
  running: "bg-secondary-container text-on-secondary-container",
  completed: "bg-primary text-on-primary",
  failed: "bg-error text-on-error",
  paused: "border border-outline text-outline",
};

const candidateStatusColor: Record<string, string> = {
  promoted: "text-primary",
  evaluated: "text-secondary-container",
  failed: "text-error",
};

const eventDotColor: Record<string, string> = {
  iteration_start: "bg-primary",
  iteration_end: "bg-secondary-container",
  candidate_created: "bg-tertiary-container",
  candidate_promoted: "bg-primary",
  candidate_failed: "bg-error",
  mutation: "bg-secondary-container",
  error: "bg-error",
};

type Tab = "overview" | "candidates" | "metrics" | "events";
const tabs: { key: Tab; label: string }[] = [
  { key: "overview", label: "Overview" },
  { key: "candidates", label: "Candidates" },
  { key: "metrics", label: "Metrics" },
  { key: "events", label: "Events" },
];

/* ── Helpers ── */

function formatDate(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleDateString(undefined, {
    month: "short",
    day: "numeric",
    year: "numeric",
    hour: "2-digit",
    minute: "2-digit",
  });
}

function formatTime(iso: string): string {
  const d = new Date(iso);
  return d.toLocaleTimeString(undefined, {
    hour: "2-digit",
    minute: "2-digit",
    second: "2-digit",
  });
}

/* ── Component ── */

export default function RunDetailClient({ runId }: { runId: string }) {
  const [run, setRun] = useState<RunDetail | null>(null);
  const [candidates, setCandidates] = useState<Candidate[]>([]);
  const [metrics, setMetrics] = useState<Metric[]>([]);
  const [events, setEvents] = useState<ResearchEvent[]>([]);
  const [activeTab, setActiveTab] = useState<Tab>("overview");
  const [expandedEvents, setExpandedEvents] = useState<Set<string>>(new Set());
  const [mutationReason, setMutationReason] = useState("");

  const fetchAll = useCallback(async () => {
    const [runRes, candRes, metRes, evtRes] = await Promise.allSettled([
      getRun(runId),
      listCandidates(runId),
      listMetrics(runId),
      listEvents(runId),
    ]);
    if (runRes.status === "fulfilled") setRun(runRes.value.run);
    if (candRes.status === "fulfilled") setCandidates(candRes.value.candidates);
    if (metRes.status === "fulfilled") setMetrics(metRes.value.metrics);
    if (evtRes.status === "fulfilled") setEvents(evtRes.value.events);
  }, [runId]);

  useEffect(() => {
    fetchAll();
  }, [fetchAll]);

  const onSSEEvent = useCallback(
    (_event: SSEEvent) => {
      fetchAll();
    },
    [fetchAll],
  );

  const { connected } = useRunSSE(runId, onSSEEvent);

  const handleControl = async (action: "pause" | "resume" | "stop") => {
    try {
      const { run: updated } = await controlRun(runId, action);
      setRun((prev) => (prev ? { ...prev, ...updated } : prev));
    } catch {
      // next SSE event will reconcile
    }
  };

  const handleRequestMutation = async () => {
    try {
      await requestMutation(runId, mutationReason);
      setMutationReason("");
    } catch {
      // silent
    }
  };

  const toggleEventExpand = (id: string) => {
    setExpandedEvents((prev) => {
      const next = new Set(prev);
      if (next.has(id)) next.delete(id);
      else next.add(id);
      return next;
    });
  };

  const progress =
    run && run.max_iterations > 0
      ? (run.current_iteration / run.max_iterations) * 100
      : 0;

  const latestCandidate = candidates.length > 0 ? candidates[candidates.length - 1] : null;

  if (!run) {
    return (
      <Shell>
        <div className="flex items-center justify-center h-full">
          <Icon name="hourglass_empty" className="text-4xl text-outline animate-pulse" />
        </div>
      </Shell>
    );
  }

  return (
    <Shell>
      <div className="flex h-full overflow-hidden">
        {/* Left panel — Chat */}
        <div className="hidden lg:flex lg:w-2/5 border-r border-outline-variant/20">
          <ChatPanel runId={runId} />
        </div>

        {/* Right panel — Dashboard */}
        <div className="flex-1 overflow-y-auto p-6">
          {/* Run header */}
          <div className="mb-6">
            <div className="flex items-center justify-between gap-3 mb-2">
              <h1 className="font-headline text-2xl font-bold tracking-tight text-on-surface truncate">
                {run.title}
              </h1>
              <div className="flex items-center gap-2 shrink-0">
                <span
                  className={`w-2 h-2 rounded-full ${connected ? "bg-primary" : "bg-error"}`}
                  title={connected ? "Live" : "Disconnected"}
                />
                <span
                  className={`px-2 py-0.5 text-[10px] font-label uppercase rounded-full ${
                    statusStyles[run.status] ?? statusStyles.paused
                  }`}
                >
                  {run.status}
                </span>
              </div>
            </div>

            <p className="text-[10px] font-label uppercase text-outline mb-3">
              {run.phase}
            </p>

            <div className="mb-4">
              <div className="flex items-center justify-between mb-1">
                <span className="text-[10px] text-outline">Progress</span>
                <span className="text-[10px] text-outline">
                  {run.current_iteration}/{run.max_iterations}
                </span>
              </div>
              <div className="h-1.5 bg-surface-container-highest rounded-full">
                <div
                  className="h-full bg-secondary-container shadow-[0_0_15px_rgba(255,191,0,0.4)] rounded-full transition-all"
                  style={{ width: `${Math.min(progress, 100)}%` }}
                />
              </div>
            </div>

            <div className="flex items-center gap-2 flex-wrap">
              {run.status === "running" && (
                <button
                  onClick={() => handleControl("pause")}
                  className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs font-label bg-surface-container-low text-on-surface-variant hover:bg-surface-container-highest transition-colors"
                >
                  <Icon name="pause" className="text-sm" />
                  Pause
                </button>
              )}
              {run.status === "paused" && (
                <button
                  onClick={() => handleControl("resume")}
                  className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs font-label bg-surface-container-low text-on-surface-variant hover:bg-surface-container-highest transition-colors"
                >
                  <Icon name="play_arrow" className="text-sm" />
                  Resume
                </button>
              )}
              {(run.status === "running" || run.status === "paused") && (
                <button
                  onClick={() => handleControl("stop")}
                  className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs font-label bg-surface-container-low text-error hover:bg-error/10 transition-colors"
                >
                  <Icon name="stop" className="text-sm" />
                  Stop
                </button>
              )}
              {(run.status === "running" || run.status === "paused") && (
                <div className="flex items-center gap-1 ml-auto">
                  <input
                    type="text"
                    placeholder="Mutation reason..."
                    value={mutationReason}
                    onChange={(e) => setMutationReason(e.target.value)}
                    className="px-2 py-1.5 rounded-lg text-xs bg-surface-container-low border border-outline-variant/20 text-on-surface placeholder:text-outline focus:outline-none focus:border-primary"
                  />
                  <button
                    onClick={handleRequestMutation}
                    className="inline-flex items-center gap-1 px-3 py-1.5 rounded-lg text-xs font-label bg-tertiary-container text-on-tertiary-container hover:bg-tertiary-container/80 transition-colors"
                  >
                    <Icon name="science" className="text-sm" />
                    Mutate
                  </button>
                </div>
              )}
            </div>
          </div>

          {/* Tab bar */}
          <div className="flex gap-4 border-b border-outline-variant/20 mb-6">
            {tabs.map((tab) => (
              <button
                key={tab.key}
                onClick={() => setActiveTab(tab.key)}
                className={`pb-2 font-label text-xs uppercase tracking-widest transition-colors ${
                  activeTab === tab.key
                    ? "text-primary border-b-2 border-primary-container"
                    : "text-outline hover:text-on-surface-variant"
                }`}
              >
                {tab.label}
              </button>
            ))}
          </div>

          {/* Tab content */}
          {activeTab === "overview" && (
            <div className="space-y-4">
              <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
                <div className="bg-surface-container-low p-4 rounded-lg">
                  <p className="text-[10px] font-label uppercase text-outline mb-1">Iteration</p>
                  <p className="font-headline text-lg text-on-surface">
                    {run.current_iteration}
                    <span className="text-xs text-outline">/{run.max_iterations}</span>
                  </p>
                </div>
                <div className="bg-surface-container-low p-4 rounded-lg">
                  <p className="text-[10px] font-label uppercase text-outline mb-1">Events</p>
                  <p className="font-headline text-lg text-on-surface">{run.event_count}</p>
                </div>
                <div className="bg-surface-container-low p-4 rounded-lg">
                  <p className="text-[10px] font-label uppercase text-outline mb-1">Status</p>
                  <p className="font-headline text-lg text-on-surface capitalize">{run.status}</p>
                </div>
                <div className="bg-surface-container-low p-4 rounded-lg">
                  <p className="text-[10px] font-label uppercase text-outline mb-1">Phase</p>
                  <p className="font-headline text-lg text-on-surface capitalize">{run.phase}</p>
                </div>
              </div>

              {latestCandidate && (
                <div className="bg-surface-container-low p-4 rounded-lg border-l-2 border-primary/30">
                  <p className="text-[10px] font-label uppercase text-outline mb-2">Latest Candidate</p>
                  <div className="flex items-center justify-between gap-2 mb-1">
                    <h4 className="font-headline text-sm font-semibold text-on-surface truncate">
                      {latestCandidate.title}
                    </h4>
                    <span className={`text-[10px] font-label uppercase ${candidateStatusColor[latestCandidate.status] ?? "text-outline"}`}>
                      {latestCandidate.status}
                    </span>
                  </div>
                  <p className="text-xs text-outline">Iteration {latestCandidate.iteration}</p>
                  <p className="text-xs text-on-surface-variant mt-1 line-clamp-2">{latestCandidate.summary}</p>
                </div>
              )}

              {run.goal && (
                <div className="bg-surface-container-low p-4 rounded-lg">
                  <p className="text-[10px] font-label uppercase text-outline mb-2">Goal</p>
                  <p className="text-xs text-on-surface-variant">{run.goal}</p>
                </div>
              )}

              {run.last_error && (
                <div className="bg-error/10 p-4 rounded-lg border-l-2 border-error">
                  <p className="text-[10px] font-label uppercase text-error mb-1">Last Error</p>
                  <p className="text-xs text-error">{run.last_error}</p>
                </div>
              )}
            </div>
          )}

          {activeTab === "candidates" && (
            <div className="space-y-3">
              {candidates.length === 0 ? (
                <div className="text-center py-12">
                  <Icon name="science" className="text-4xl text-outline mb-3 block mx-auto" />
                  <p className="text-outline text-sm">No candidates yet</p>
                </div>
              ) : (
                candidates.map((c) => (
                  <div
                    key={c.id}
                    className={`bg-surface-container-low p-4 rounded-lg ${
                      c.status === "promoted" ? "border-l-2 border-secondary-container/50" : ""
                    }`}
                  >
                    <div className="flex items-center justify-between gap-2 mb-1">
                      <div className="flex items-center gap-2 min-w-0">
                        <span className="text-[10px] font-label text-outline shrink-0">#{c.iteration}</span>
                        <h4 className="font-headline text-sm font-semibold text-on-surface truncate">{c.title}</h4>
                      </div>
                      <span className={`shrink-0 text-[10px] font-label uppercase ${candidateStatusColor[c.status] ?? "text-outline"}`}>
                        {c.status}
                      </span>
                    </div>
                    <p className="text-xs text-on-surface-variant line-clamp-2">{c.summary}</p>
                    <p className="text-[10px] text-outline mt-1">{formatDate(c.created_at)}</p>
                  </div>
                ))
              )}
            </div>
          )}

          {activeTab === "metrics" && (
            <div className="space-y-6">
              <MetricsChart metrics={metrics} />
              {metrics.length > 0 && (
                <div className="bg-surface-container-low rounded-lg overflow-hidden">
                  <table className="w-full text-xs">
                    <thead>
                      <tr className="border-b border-outline-variant/20">
                        <th className="text-left px-4 py-2 font-label uppercase text-[10px] text-outline tracking-widest">Iteration</th>
                        <th className="text-left px-4 py-2 font-label uppercase text-[10px] text-outline tracking-widest">Name</th>
                        <th className="text-right px-4 py-2 font-label uppercase text-[10px] text-outline tracking-widest">Value</th>
                      </tr>
                    </thead>
                    <tbody>
                      {metrics.map((m) => (
                        <tr key={m.id} className="border-b border-outline-variant/10 last:border-0">
                          <td className="px-4 py-2 text-on-surface-variant">{m.iteration}</td>
                          <td className="px-4 py-2 text-on-surface">{m.name}</td>
                          <td className="px-4 py-2 text-on-surface text-right font-mono">
                            {typeof m.value === "number" ? m.value.toFixed(4) : m.value}
                          </td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                </div>
              )}
              {metrics.length === 0 && (
                <div className="text-center py-12">
                  <Icon name="monitoring" className="text-4xl text-outline mb-3 block mx-auto" />
                  <p className="text-outline text-sm">No metrics recorded yet</p>
                </div>
              )}
            </div>
          )}

          {activeTab === "events" && (
            <div className="space-y-0">
              {events.length === 0 ? (
                <div className="text-center py-12">
                  <Icon name="timeline" className="text-4xl text-outline mb-3 block mx-auto" />
                  <p className="text-outline text-sm">No events yet</p>
                </div>
              ) : (
                <div className="relative ml-3 border-l border-outline-variant/20">
                  {events.map((evt) => {
                    const isExpanded = expandedEvents.has(evt.id);
                    return (
                      <div key={evt.id} className="relative pl-6 pb-4">
                        <span
                          className={`absolute left-0 top-1.5 -translate-x-1/2 w-2.5 h-2.5 rounded-full ring-2 ring-surface-container-lowest ${
                            eventDotColor[evt.event_type] ?? "bg-outline"
                          }`}
                        />
                        <button onClick={() => toggleEventExpand(evt.id)} className="w-full text-left">
                          <div className="flex items-center gap-2 mb-0.5">
                            <span className="text-[10px] font-label uppercase font-semibold text-on-surface">
                              {evt.event_type}
                            </span>
                            <span className="text-[10px] text-outline">{formatTime(evt.timestamp)}</span>
                            <Icon
                              name={isExpanded ? "expand_less" : "expand_more"}
                              className="text-sm text-outline ml-auto"
                            />
                          </div>
                        </button>
                        {isExpanded && (
                          <pre className="mt-1 p-3 bg-surface-container-low rounded-lg text-[10px] text-on-surface-variant overflow-x-auto">
                            {JSON.stringify(evt.payload, null, 2)}
                          </pre>
                        )}
                      </div>
                    );
                  })}
                </div>
              )}
            </div>
          )}
        </div>
      </div>
    </Shell>
  );
}
