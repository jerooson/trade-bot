import {
  Area,
  AreaChart,
  ResponsiveContainer,
  Tooltip,
  XAxis,
  YAxis,
} from "recharts";
import type { Stats } from "../lib/types";
import { SectionHeader } from "./SectionHeader";

interface Props {
  stats: Stats;
}

export function DailyChart({ stats }: Props) {
  const data = stats.by_day.map((d) => ({
    date: d.date,
    label: d.date.slice(5),
    count: d.count,
  }));

  return (
    <section>
      <SectionHeader
        index="04"
        label="signal flux · by day"
        hint="cumulative pulse · range of capture"
        right={<span className="tabular">{data.length}d</span>}
      />

      <div className="h-44 px-2 pt-4">
        <ResponsiveContainer width="100%" height="100%">
          <AreaChart data={data} margin={{ top: 6, right: 8, bottom: 0, left: 0 }}>
            <defs>
              <linearGradient id="dailyGradient" x1="0" y1="0" x2="0" y2="1">
                <stop offset="0%" stopColor="#ffb800" stopOpacity={0.5} />
                <stop offset="100%" stopColor="#ffb800" stopOpacity={0} />
              </linearGradient>
            </defs>
            <XAxis
              dataKey="label"
              tick={{ fill: "#5a5a62", fontSize: 10, fontFamily: "JetBrains Mono" }}
              axisLine={{ stroke: "#26262c" }}
              tickLine={{ stroke: "#26262c" }}
              interval="preserveStartEnd"
            />
            <YAxis
              tick={{ fill: "#5a5a62", fontSize: 10, fontFamily: "JetBrains Mono" }}
              axisLine={{ stroke: "#26262c" }}
              tickLine={{ stroke: "#26262c" }}
              width={32}
            />
            <Tooltip
              cursor={{ stroke: "rgba(255,184,0,0.4)" }}
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
              formatter={(value: number) => [value, "signals"]}
              labelFormatter={(label: string) => `date · 2026-${label}`}
            />
            <Area
              type="monotone"
              dataKey="count"
              stroke="#ffb800"
              strokeWidth={1.5}
              fill="url(#dailyGradient)"
            />
          </AreaChart>
        </ResponsiveContainer>
      </div>
    </section>
  );
}
