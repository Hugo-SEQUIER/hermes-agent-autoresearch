"use client";

import { useMemo } from "react";
import {
  ResponsiveContainer,
  LineChart,
  Line,
  XAxis,
  YAxis,
  CartesianGrid,
  Tooltip,
  Legend,
} from "recharts";

interface MetricsChartProps {
  metrics: Array<{
    id: string;
    iteration: number;
    name: string;
    value: number;
    created_at: string;
  }>;
}

const COLORS = ["#bbc3ff", "#ffbf00", "#b9c3ff", "#ffb4ab", "#dde1ff", "#ffdfa0"];

export default function MetricsChart({ metrics }: MetricsChartProps) {
  const { chartData, metricNames } = useMemo(() => {
    const filtered = metrics.filter(
      (m) =>
        !m.name.startsWith("iteration_") &&
        !m.name.startsWith("mutation.") &&
        !m.name.startsWith("dataset.")
    );

    const grouped: Record<string, Array<{ iteration: number; value: number }>> = {};
    for (const m of filtered) {
      if (!grouped[m.name]) grouped[m.name] = [];
      grouped[m.name].push({ iteration: m.iteration, value: m.value });
    }

    const names = Object.keys(grouped);

    const iterationMap: Record<number, Record<string, number>> = {};
    for (const m of filtered) {
      if (!iterationMap[m.iteration]) iterationMap[m.iteration] = {};
      iterationMap[m.iteration][m.name] = m.value;
    }

    const data = Object.entries(iterationMap)
      .map(([iter, values]) => ({
        iteration: Number(iter),
        ...values,
      }))
      .sort((a, b) => a.iteration - b.iteration);

    return { chartData: data, metricNames: names };
  }, [metrics]);

  if (!metrics.length || !metricNames.length) {
    return <p className="text-outline text-sm">No metrics recorded yet</p>;
  }

  return (
    <div className="bg-surface-container-low p-6 rounded-lg border-t-2 border-primary/30">
      <h3 className="font-label text-xs uppercase tracking-[0.2em] text-outline mb-6">
        Metric Trends
      </h3>
      <ResponsiveContainer width="100%" height={300}>
        <LineChart data={chartData}>
          <CartesianGrid stroke="#313442" strokeDasharray="3 3" />
          <XAxis
            dataKey="iteration"
            stroke="#8e8fa2"
            tick={{ fill: "#8e8fa2", fontSize: 11 }}
          />
          <YAxis
            stroke="#8e8fa2"
            tick={{ fill: "#8e8fa2", fontSize: 11 }}
          />
          <Tooltip
            contentStyle={{
              background: "#1b1f2c",
              border: "1px solid #444656",
              borderRadius: 4,
            }}
            labelStyle={{ color: "#bbc3ff" }}
            itemStyle={{ color: "#dfe2f3" }}
          />
          <Legend wrapperStyle={{ fontSize: 11 }} />
          {metricNames.map((name, i) => (
            <Line
              key={name}
              type="monotone"
              dataKey={name}
              stroke={COLORS[i % COLORS.length]}
              strokeWidth={2}
              dot={false}
              activeDot={{ r: 4 }}
            />
          ))}
        </LineChart>
      </ResponsiveContainer>
    </div>
  );
}
