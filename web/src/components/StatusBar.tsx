import { useEffect, useState } from "react";
import type { ConnectionState } from "../hooks/useDashboardData";
import { fmtTime, relativeTime } from "../lib/format";
import clsx from "clsx";

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

  const dotClass = clsx("inline-block h-2 w-2 rounded-full", {
    "bg-crt-long animate-pulseDot": conn === "connected",
    "bg-crt-amber animate-pulseDot": conn === "connecting",
    "bg-crt-short": conn === "disconnected",
  });

  const stateLabel = conn === "connected" ? "ONLINE" : conn === "connecting" ? "LINKING" : "OFFLINE";

  return (
    <header className="relative border-b border-ink-500/60 bg-ink-900/80 backdrop-blur-sm">
      <div className="sweep" />
      <div className="relative z-10 mx-auto flex max-w-[1600px] items-stretch px-6">
        {/* Left: brand */}
        <div className="flex flex-col justify-center py-4 pr-10">
          <div className="text-[11px] uppercase tracking-[0.32em] text-bone-400">
            Will-the-Rocket
          </div>
          <div className="mt-0.5 flex items-baseline gap-3">
            <h1 className="text-2xl font-medium tracking-tight text-bone-50">
              SIGNAL <span className="font-editorial italic text-crt-amber">terminal</span>
            </h1>
            <span className="text-[11px] uppercase tracking-[0.18em] text-bone-500">
              v0.1.0
            </span>
          </div>
        </div>

        {/* Vertical rule */}
        <div className="rule-y my-3" />

        {/* Center: status */}
        <div className="flex items-center gap-8 px-10 py-4">
          <Stat
            label="link"
            value={
              <span className="flex items-center gap-2">
                <span className={dotClass} />
                <span
                  className={clsx({
                    "text-crt-long": conn === "connected",
                    "text-crt-amber": conn === "connecting",
                    "text-crt-short": conn === "disconnected",
                  })}
                >
                  {stateLabel}
                </span>
              </span>
            }
          />
          <Stat
            label="last event"
            value={
              <span className="text-bone-100">
                {lastEventAt ? relativeTime(lastEventAt) : "—"}
              </span>
            }
          />
          <Stat
            label={totalLabel}
            value={<span className="tabular text-bone-100">{total.toLocaleString()}</span>}
          />
        </div>

        {/* Vertical rule */}
        <div className="rule-y my-3" />

        {/* Right: clock */}
        <div className="ml-auto flex flex-col justify-center py-4 pl-10 text-right">
          <div className="text-[11px] uppercase tracking-[0.32em] text-bone-400">
            {now
              .toLocaleDateString("en-US", { weekday: "short", month: "short", day: "2-digit" })
              .toUpperCase()}
          </div>
          <div className="tabular text-2xl font-medium text-bone-50">
            {fmtTime(now.toISOString())}
          </div>
        </div>
      </div>
    </header>
  );
}

function Stat({ label, value }: { label: string; value: React.ReactNode }) {
  return (
    <div className="flex flex-col">
      <div className="text-[10px] uppercase tracking-[0.32em] text-bone-500">{label}</div>
      <div className="mt-0.5 text-sm">{value}</div>
    </div>
  );
}
