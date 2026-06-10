import { useEffect, useState } from "react";
import clsx from "clsx";
import { Bot, Clock3, Database } from "lucide-react";
import type { ConnectionState } from "../hooks/useDashboardData";
import { relativeTime } from "../lib/format";

interface Props {
  conn: ConnectionState;
  lastEventAt: string | null;
  total: number;
  totalLabel?: string;
}

export function StatusBar({ conn, lastEventAt, total, totalLabel = "signals on file" }: Props) {
  const [now, setNow] = useState<Date>(new Date());

  useEffect(() => {
    const id = setInterval(() => setNow(new Date()), 1000);
    return () => clearInterval(id);
  }, []);

  const stateLabel = conn === "connected" ? "Connected" : conn === "connecting" ? "Connecting" : "Offline";

  return (
    <header className="sticky top-0 z-40 border-b border-ink-500/30 bg-ink-950/75 backdrop-blur-xl">
      <div className="mx-auto flex max-w-[1680px] items-center gap-5 px-4 py-3 sm:px-6">
        <div className="flex min-w-0 items-center gap-3 md:w-[228px]">
          <div className="flex h-9 w-9 shrink-0 items-center justify-center rounded-xl bg-gradient-to-br from-crt-info/25 to-crt-long/15 text-crt-info ring-1 ring-crt-info/30">
            <Bot className="h-5 w-5" />
          </div>
          <div className="min-w-0">
            <h1 className="truncate text-sm font-semibold text-bone-50">Trade Operator</h1>
            <div className="truncate text-[11px] text-bone-500">Discord to broker console</div>
          </div>
        </div>

        <div className="hidden items-center gap-8 border-l border-ink-500/30 pl-6 sm:flex">
          <Stat
            label="Listener"
            value={
              <span className={clsx("flex items-center gap-2 font-medium", conn === "connected" ? "text-crt-long" : conn === "connecting" ? "text-crt-amber" : "text-crt-short")}>
                <span className={clsx("h-2 w-2 rounded-full", conn === "connected" ? "animate-pulseDot bg-crt-long" : conn === "connecting" ? "bg-crt-amber" : "bg-crt-short")} />
                {stateLabel}
              </span>
            }
          />
          <Stat label="Last event" value={lastEventAt ? relativeTime(lastEventAt) : "No events"} />
          <Stat label={totalLabel} value={total.toLocaleString()} icon />
        </div>

        <div className="ml-auto flex items-center gap-2 rounded-full border border-ink-500/40 bg-ink-900/70 px-3 py-1.5">
          <Clock3 className="h-3.5 w-3.5 text-bone-500" />
          <div className="tabular text-xs font-medium text-bone-200">
            {now.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" })}
          </div>
        </div>
      </div>
    </header>
  );
}

function Stat({ label, value, icon }: { label: string; value: React.ReactNode; icon?: boolean }) {
  return (
    <div className="flex items-center gap-2">
      {icon && <Database className="h-3.5 w-3.5 text-bone-500" />}
      <div>
        <div className="text-[9px] font-semibold uppercase tracking-[0.16em] text-bone-500">{label}</div>
        <div className="mt-0.5 text-xs text-bone-200">{value}</div>
      </div>
    </div>
  );
}
