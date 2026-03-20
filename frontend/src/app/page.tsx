"use client";

import Link from "next/link";
import { Shell } from "@/components/Shell";
import { Icon } from "@/components/Icon";
import { listRuns, Run, controlRun } from "@/lib/api";
import { usePolling } from "@/lib/useSSE";

const statusStyles: Record<string, string> = {
  running: "bg-secondary-container text-on-secondary-container",
  completed: "bg-primary text-on-primary",
  failed: "bg-error text-on-error",
  paused: "border border-outline text-outline",
};

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

function RunCard({ run }: { run: Run }) {
  const progress =
    run.max_iterations > 0
      ? (run.current_iteration / run.max_iterations) * 100
      : 0;

  const handleControl = async (
    e: React.MouseEvent,
    action: "pause" | "resume" | "stop"
  ) => {
    e.preventDefault();
    e.stopPropagation();
    try {
      await controlRun(run.id, action);
    } catch {
      // silently ignore – next poll will reflect state
    }
  };

  return (
    <Link href={`/runs/${run.id}`} className="block">
      <div className="bg-surface-container-low p-6 rounded-lg border-t-2 border-primary/30 hover:bg-surface-container-highest transition-all">
        {/* Title + status */}
        <div className="flex items-center justify-between gap-3 mb-3">
          <h3 className="font-headline text-sm font-semibold text-on-surface truncate">
            {run.title}
          </h3>
          <span
            className={`shrink-0 px-2 py-0.5 text-[10px] font-label uppercase rounded-full ${
              statusStyles[run.status] ?? statusStyles.paused
            }`}
          >
            {run.status}
          </span>
        </div>

        {/* Phase */}
        <p className="text-[10px] font-label uppercase text-outline mb-3">
          {run.phase}
        </p>

        {/* Progress bar */}
        <div className="mb-3">
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

        {/* Goal */}
        <p className="text-xs text-outline line-clamp-2 mb-3">{run.goal}</p>

        {/* Footer: timestamp + controls */}
        <div className="flex items-center justify-between">
          <span className="text-[10px] text-outline">
            {formatDate(run.created_at)}
          </span>

          <div className="flex items-center gap-1">
            {run.status === "running" && (
              <button
                onClick={(e) => handleControl(e, "pause")}
                className="text-on-surface-variant hover:text-primary transition-colors"
                title="Pause"
              >
                <Icon name="pause" className="text-base" />
              </button>
            )}
            {run.status === "paused" && (
              <button
                onClick={(e) => handleControl(e, "resume")}
                className="text-on-surface-variant hover:text-primary transition-colors"
                title="Resume"
              >
                <Icon name="play_arrow" className="text-base" />
              </button>
            )}
            {(run.status === "running" || run.status === "paused") && (
              <button
                onClick={(e) => handleControl(e, "stop")}
                className="text-on-surface-variant hover:text-error transition-colors"
                title="Stop"
              >
                <Icon name="stop" className="text-base" />
              </button>
            )}
          </div>
        </div>
      </div>
    </Link>
  );
}

export default function HomePage() {
  const { data } = usePolling(listRuns, 5000);
  const runs: Run[] = data?.runs ?? [];

  return (
    <Shell>
      <div className="p-8 max-w-6xl mx-auto">
        {/* Header */}
        <div className="flex items-center justify-between mb-8">
          <h1 className="font-headline text-2xl font-bold tracking-tight text-on-surface">
            Active Synthesis
          </h1>
          <Link
            href="/runs"
            className="inline-flex items-center gap-2 px-4 py-2 text-outline hover:text-primary text-sm font-label transition-colors"
          >
            <Icon name="history" className="text-lg" />
            All Runs
          </Link>
        </div>

        {/* Run grid */}
        {runs.length > 0 ? (
          <div className="grid grid-cols-1 md:grid-cols-2 gap-4">
            {runs.map((run) => (
              <RunCard key={run.id} run={run} />
            ))}
          </div>
        ) : (
          <div className="glass-panel rounded-lg p-12 text-center">
            <Icon
              name="science"
              className="text-4xl text-outline mb-3 block mx-auto"
            />
            <p className="text-outline text-sm">No active research runs</p>
          </div>
        )}
      </div>
    </Shell>
  );
}
