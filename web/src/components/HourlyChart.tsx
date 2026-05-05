import {
  Bar,
  BarChart,
  Cell,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { Stats } from "../lib/types";
import { SectionHeader } from "./SectionHeader";

interface Props {
  stats: Stats;
  singleDay: boolean;
}

export function HourlyChart({ stats, singleDay }: Props) {
  const data = stats.by_hour_utc.map((d) => ({
    hour: d.hour,
    label: String(d.hour).padStart(2, "0"),
    count: d.count,
  }));

  const peakHour = data.reduce(
    (peak, d) => (d.count > peak.count ? d : peak),
    { hour: -1, count: 0 } as { hour: number; count: number; label?: string },
  );

  const totalCount = data.reduce((s, d) => s + d.count, 0);

  return (
    <section>
      <SectionHeader
        index="03"
        label={singleDay ? "intraday flux · session" : "signal flux · hour of day"}
        hint={
          totalCount === 0
            ? "no events in range"
            : peakHour.hour >= 0
              ? `peak ${String(peakHour.hour).padStart(2, "0")}:00 utc · ${peakHour.count} events`
              : "all hours · utc"
        }
        right={<span className="tabular">utc</span>}
      />

      <div className="h-48 px-2 pt-4">
        {totalCount === 0 ? (
          <div className="flex h-full items-center justify-center text-[10px] uppercase tracking-[0.32em] text-bone-500">
            no events to plot
          </div>
        ) : (
          <ResponsiveContainer width="100%" height="100%">
            <BarChart data={data} margin={{ top: 4, right: 8, bottom: 0, left: 0 }}>
              <XAxis
                dataKey="hour"
                tick={{ fill: "#5a5a62", fontSize: 10, fontFamily: "JetBrains Mono" }}
                axisLine={{ stroke: "#26262c" }}
                tickLine={{ stroke: "#26262c" }}
                interval={1}
              />
              <YAxis
                tick={{ fill: "#5a5a62", fontSize: 10, fontFamily: "JetBrains Mono" }}
                axisLine={{ stroke: "#26262c" }}
                tickLine={{ stroke: "#26262c" }}
                width={32}
                allowDecimals={false}
              />
              <Tooltip
                cursor={{ fill: "rgba(255,184,0,0.08)" }}
                contentStyle={{
                  background: "#0f0f12",
                  border: "1px solid #26262c",
                  fontFamily: "JetBrains Mono",
                  fontSize: 11,
                  textTransform: "uppercase",
                  letterSpacing: "0.05em",
                  color: "#e8e8ea",
                }}
                labelStyle={{ color: "#7a7a82" }}
                formatter={(value: number) => [value, "events"]}
                labelFormatter={(label: number) => `hour ${String(label).padStart(2, "0")}:00 utc`}
              />
              <Bar dataKey="count" radius={[1, 1, 0, 0]}>
                {data.map((d) => (
                  <Cell
                    key={d.hour}
                    fill={d.count > 0 && d.count === peakHour.count ? "#ffb800" : "#3a3a42"}
                  />
                ))}
              </Bar>
            </BarChart>
          </ResponsiveContainer>
        )}
      </div>
    </section>
  );
}
