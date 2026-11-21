"use client";

/**
 * Lazily loaded charts and graphs. recharts and @xyflow/react are large; screens import these wrappers so the
 * libraries download only when a chart or graph actually renders (a tab is opened, an answer arrives), not with
 * the route's first paint. Each wrapper shows a skeleton of the right size meanwhile.
 */

import dynamic from "next/dynamic";
import { Skeleton } from "@/components/ui/feedback";

const box = (h: number) =>
  function ChartLoading() {
    return <Skeleton className="w-full rounded-control" style={{ height: h }} />;
  };

/* recharts: Ask AI / dashboards */
export const ResultChart = dynamic(() => import("@/components/ask/result-chart").then((m) => m.ResultChart), { ssr: false, loading: box(280) });

/* recharts: overview + dataset profile */
export const RunsDonut = dynamic(() => import("@/components/dashboard/charts").then((m) => m.RunsDonut), { ssr: false, loading: box(220) });
export const RowsByDatasetChart = dynamic(() => import("@/components/dashboard/charts").then((m) => m.RowsByDatasetChart), { ssr: false, loading: box(240) });
export const HistogramChart = dynamic(() => import("@/components/dashboard/charts").then((m) => m.HistogramChart), { ssr: false, loading: box(160) });

/* recharts: ML workspace */
export const CorrelationHeatmap = dynamic(() => import("@/components/ml/ml-charts").then((m) => m.CorrelationHeatmap), { ssr: false, loading: box(320) });
export const ConfusionHeatmap = dynamic(() => import("@/components/ml/ml-charts").then((m) => m.ConfusionHeatmap), { ssr: false, loading: box(260) });
export const CurveChart = dynamic(() => import("@/components/ml/ml-charts").then((m) => m.CurveChart), { ssr: false, loading: box(260) });
export const ResidualScatter = dynamic(() => import("@/components/ml/ml-charts").then((m) => m.ResidualScatter), { ssr: false, loading: box(260) });
export const HBarChart = dynamic(() => import("@/components/ml/ml-charts").then((m) => m.HBarChart), { ssr: false, loading: box(240) });
export const CountBars = dynamic(() => import("@/components/ml/ml-charts").then((m) => m.CountBars), { ssr: false, loading: box(220) });
export const MetricBarsChart = dynamic(() => import("@/components/ml/ml-charts").then((m) => m.MetricBarsChart), { ssr: false, loading: box(240) });
export const PsiBars = dynamic(() => import("@/components/ml/ml-charts").then((m) => m.PsiBars), { ssr: false, loading: box(220) });
export const DistributionCompare = dynamic(() => import("@/components/ml/ml-charts").then((m) => m.DistributionCompare), { ssr: false, loading: box(220) });
export const ContributionBars = dynamic(() => import("@/components/ml/ml-charts").then((m) => m.ContributionBars), { ssr: false, loading: box(200) });

/* @xyflow/react: dataset lineage tab */
export const LineageGraphView = dynamic(() => import("@/components/lineage/lineage-graph").then((m) => m.LineageGraphView), { ssr: false, loading: box(420) });
