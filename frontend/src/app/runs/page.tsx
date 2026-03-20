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

function RunRow({ run }: { run: Run }) {
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
      // next poll will reflect state
    }
  };

  return (
    <Link href={`/runs/${run.id}`} className="block">
      <div className="flex items-center gap-4 px-4 py-3 bg-surface-container-low rounded-lg hover:bg-surface-container-highest transition-all">
        {/* Status badge */}
        <span
          className={`shrink-0 w-20 text-center px-2 py-0.5 text-[10px] font-label uppercase rounded-full ${
            statusStyles[run.status] ?? statusStyles.paused
          }`}
        >
          {run.status}
        </span>

        {/* Title + goal */}
        <div className="flex-1 min-w-0">
          <h3 className="font-headline text-sm font-semibold text-on-surface truncate">
            {run.title}
          </h3>
          <p className="text-xs text-outline truncate">{run.goal}</p>
        </div>

        {/* Progress */}
        <div className="hidden sm:flex items-center gap-2 w-36 shrink-0">
          <div className="flex-1 h-1.5 bg-surface-container-highest rounded-full">
            <div
              className="h-full bg-secondary-container rounded-full transition-all"
              style={{ width: `${Math.min(progress, 100)}%` }}
            />
          </div>
          <span className="text-[10px] text-outline whitespace-nowrap">
            {run.current_iteration}/{run.max_iterations}
          </span>
        </div>

        {/* Date */}
        <span className="hidden md:block text-[10px] text-outline whitespace-nowrap w-32 text-right">
          {formatDate(run.created_at)}
        </span>

        {/* Controls */}
        <div className="flex items-center gap-1 w-14 justify-end shrink-0">
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
    </Link>
  );
}

export default function RunsPage() {
  const { data } = usePolling(listRuns, 5000);
  const runs: Run[] = data?.runs ?? [];

  const active = runs.filter(
    (r) => r.status === "running" || r.status === "paused"
  );
  const finished = runs.filter(
    (r) => r.status !== "running" && r.status !== "paused"
  );

  return (
    <Shell>
      <div className="p-8 max-w-5xl mx-auto">
        <h1 className="font-headline text-2xl font-bold tracking-tight text-on-surface mb-8">
          Research Runs
        </h1>

        {runs.length === 0 ? (
          <div className="glass-panel rounded-lg p-12 text-center">
            <Icon
              name="science"
              className="text-4xl text-outline mb-3 block mx-auto"
            />
            <p className="text-outline text-sm">No research runs yet</p>
          </div>
        ) : (
          <>
            {active.length > 0 && (
              <section className="mb-8">
                <h2 className="text-xs font-label uppercase tracking-widest text-outline mb-3">
                  Active ({active.length})
                </h2>
                <div className="space-y-2">
                  {active.map((run) => (
                    <RunRow key={run.id} run={run} />
                  ))}
                </div>
              </section>
            )}

            {finished.length > 0 && (
              <section>
                <h2 className="text-xs font-label uppercase tracking-widest text-outline mb-3">
                  Completed ({finished.length})
                </h2>
                <div className="space-y-2">
                  {finished.map((run) => (
                    <RunRow key={run.id} run={run} />
                  ))}
                </div>
              </section>
            )}
          </>
        )}
      </div>
    </Shell>
  );
}
