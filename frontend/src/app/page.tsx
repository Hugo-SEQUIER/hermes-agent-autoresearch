"use client";

import Link from "next/link";
import { Shell } from "@/components/Shell";
import { Icon } from "@/components/Icon";
import ChatPanel from "@/components/ChatPanel";
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
      <div className="bg-surface-container-low p-4 rounded-lg border-l-2 border-primary/30 hover:bg-surface-container-highest transition-all">
        <div className="flex items-center justify-between gap-2 mb-2">
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

        <div className="mb-2">
          <div className="flex items-center justify-between mb-1">
            <span className="text-[10px] text-outline">{run.phase}</span>
            <span className="text-[10px] text-outline">
              {run.current_iteration}/{run.max_iterations}
            </span>
          </div>
          <div className="h-1 bg-surface-container-highest rounded-full">
            <div
              className="h-full bg-secondary-container rounded-full transition-all"
              style={{ width: `${Math.min(progress, 100)}%` }}
            />
          </div>
        </div>

        <p className="text-xs text-outline line-clamp-1 mb-2">{run.goal}</p>

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
                <Icon name="pause" className="text-sm" />
              </button>
            )}
            {run.status === "paused" && (
              <button
                onClick={(e) => handleControl(e, "resume")}
                className="text-on-surface-variant hover:text-primary transition-colors"
                title="Resume"
              >
                <Icon name="play_arrow" className="text-sm" />
              </button>
            )}
            {(run.status === "running" || run.status === "paused") && (
              <button
                onClick={(e) => handleControl(e, "stop")}
                className="text-on-surface-variant hover:text-error transition-colors"
                title="Stop"
              >
                <Icon name="stop" className="text-sm" />
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
      <div className="flex h-full overflow-hidden">
        {/* Left panel — Chat with Hermes */}
        <div className="flex w-full lg:w-1/2 xl:w-2/5">
          <ChatPanel />
        </div>

        {/* Right panel — Active runs */}
        <div className="hidden lg:block lg:w-1/2 xl:w-3/5 overflow-y-auto border-l border-outline-variant/20">
          <div className="p-6">
            <div className="flex items-center justify-between mb-4">
              <h2 className="font-headline text-lg font-bold tracking-tight text-on-surface">
                Active Runs
              </h2>
              <Link
                href="/runs"
                className="inline-flex items-center gap-1 text-xs text-outline hover:text-primary font-label uppercase tracking-widest transition-colors"
              >
                <Icon name="history" className="text-sm" />
                All Runs
              </Link>
            </div>

            {runs.length > 0 ? (
              <div className="space-y-3">
                {runs.map((run) => (
                  <RunCard key={run.id} run={run} />
                ))}
              </div>
            ) : (
              <div className="rounded-lg p-8 text-center bg-surface-container-low">
                <Icon
                  name="science"
                  className="text-3xl text-outline mb-2 block mx-auto"
                />
                <p className="text-outline text-sm mb-1">No research runs yet</p>
                <p className="text-outline/50 text-xs">
                  Ask Hermes to start a new research run
                </p>
              </div>
            )}
          </div>
        </div>
      </div>
    </Shell>
  );
}
