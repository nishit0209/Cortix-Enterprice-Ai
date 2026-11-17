"use client";

import * as React from "react";
import Link from "next/link";
import { motion, useReducedMotion } from "framer-motion";
import { Database, GitBranch, ShieldCheck, Sparkles } from "lucide-react";
import { Logo, LogoMark } from "@/components/brand/logo";

const VALUE_PROPS = [
  {
    icon: Database,
    title: "One governed data workspace",
    text: "Upload, profile and score data quality with every change audited.",
  },
  {
    icon: Sparkles,
    title: "Inspectable AI answers",
    text: "Every answer shows its SQL, sources and the policy that applied.",
  },
  {
    icon: ShieldCheck,
    title: "Privacy by default",
    text: "New organizations only send synthetic data to Gemini until an admin consents.",
  },
];

/** Abstract brand visual: a governed data graph. Decorative only. */
function DataConstellation() {
  const reduce = useReducedMotion();
  const nodes = [
    { x: 70, y: 70, r: 5 },
    { x: 190, y: 40, r: 4 },
    { x: 300, y: 95, r: 6 },
    { x: 120, y: 170, r: 4 },
    { x: 245, y: 190, r: 5 },
    { x: 360, y: 210, r: 4 },
    { x: 60, y: 260, r: 4 },
    { x: 180, y: 290, r: 6 },
    { x: 320, y: 300, r: 4 },
  ];
  const edges = [
    [0, 1],
    [1, 2],
    [0, 3],
    [3, 4],
    [2, 4],
    [4, 5],
    [3, 6],
    [6, 7],
    [4, 7],
    [7, 8],
    [5, 8],
  ];
  return (
    <svg viewBox="0 0 420 340" className="h-auto w-full max-w-[460px]" aria-hidden>
      <defs>
        <linearGradient id="edge" x1="0" y1="0" x2="1" y2="1">
          <stop offset="0" stopColor="#a5a1ff" stopOpacity="0.55" />
          <stop offset="1" stopColor="#5eead4" stopOpacity="0.45" />
        </linearGradient>
        <radialGradient id="glow">
          <stop offset="0" stopColor="#8b87ff" stopOpacity="0.55" />
          <stop offset="1" stopColor="#8b87ff" stopOpacity="0" />
        </radialGradient>
      </defs>
      {edges.map(([a, b], i) => (
        <motion.line
          key={i}
          x1={nodes[a].x}
          y1={nodes[a].y}
          x2={nodes[b].x}
          y2={nodes[b].y}
          stroke="url(#edge)"
          strokeWidth={1.2}
          initial={reduce ? false : { pathLength: 0, opacity: 0 }}
          animate={{ pathLength: 1, opacity: 1 }}
          transition={{ duration: 0.9, delay: 0.15 + i * 0.06, ease: "easeOut" }}
        />
      ))}
      {nodes.map((n, i) => (
        <g key={i}>
          <circle cx={n.x} cy={n.y} r={n.r * 4} fill="url(#glow)" />
          <motion.circle
            cx={n.x}
            cy={n.y}
            r={n.r}
            fill={i % 3 === 2 ? "#5eead4" : "#c4c2ff"}
            initial={reduce ? false : { scale: 0, opacity: 0 }}
            animate={{ scale: 1, opacity: 1 }}
            transition={{ duration: 0.3, delay: 0.1 + i * 0.05 }}
            style={{ transformOrigin: `${n.x}px ${n.y}px` }}
          />
        </g>
      ))}
    </svg>
  );
}

export function AuthShell({ children }: { children: React.ReactNode }) {
  return (
    <div className="grid min-h-dvh bg-bg lg:grid-cols-[minmax(0,1.05fr)_minmax(0,1fr)]">
      {/* Brand panel */}
      <aside className="relative hidden overflow-hidden bg-[#0b0c1a] text-white lg:flex lg:flex-col">
        <div
          className="absolute inset-0 opacity-[0.55]"
          style={{
            background:
              "radial-gradient(60% 50% at 20% 10%, rgba(99,91,255,0.55) 0%, transparent 70%), radial-gradient(50% 45% at 90% 90%, rgba(20,184,166,0.35) 0%, transparent 70%)",
          }}
          aria-hidden
        />
        <div
          className="absolute inset-0 opacity-[0.07]"
          style={{
            backgroundImage:
              "linear-gradient(to right, #fff 1px, transparent 1px), linear-gradient(to bottom, #fff 1px, transparent 1px)",
            backgroundSize: "40px 40px",
            maskImage: "radial-gradient(ellipse at center, black 30%, transparent 75%)",
          }}
          aria-hidden
        />
        <div className="relative z-10 flex flex-1 flex-col justify-between p-10 xl:p-14">
          <div className="flex items-center gap-2.5">
            <LogoMark size={30} />
            <span className="text-[15px] font-semibold tracking-[0.14em]">NEXORA</span>
          </div>
          <div className="flex flex-col items-start gap-10">
            <DataConstellation />
            <div className="max-w-md space-y-3">
              <h2 className="text-2xl font-semibold leading-tight tracking-[-0.02em]">
                Your data, governed end to end. <span className="text-[#a5a1ff]">AI you can inspect.</span>
              </h2>
              <p className="text-sm leading-6 text-white/65">
                NEXORA brings data engineering, quality, conversational BI and Gemini-powered insight into one secure,
                multi-tenant workspace.
              </p>
            </div>
            <ul className="grid max-w-md gap-4">
              {VALUE_PROPS.map(({ icon: Icon, title, text }) => (
                <li key={title} className="flex gap-3">
                  <span className="mt-0.5 flex size-8 shrink-0 items-center justify-center rounded-[8px] border border-white/10 bg-white/5">
                    <Icon className="size-4 text-[#c4c2ff]" aria-hidden />
                  </span>
                  <span>
                    <span className="block text-sm font-medium">{title}</span>
                    <span className="block text-sm text-white/55">{text}</span>
                  </span>
                </li>
              ))}
            </ul>
          </div>
          <div className="flex items-center gap-2 text-xs text-white/40">
            <GitBranch className="size-3.5" aria-hidden />
            Tenant-isolated by design · Every mutation audited
          </div>
        </div>
      </aside>

      {/* Form panel */}
      <main className="flex min-h-dvh flex-col">
        <div className="flex h-16 items-center px-6 lg:hidden">
          <Link href="/" aria-label="NEXORA home">
            <Logo />
          </Link>
        </div>
        <div className="flex flex-1 items-center justify-center px-4 py-10 sm:px-8">
          <div className="w-full max-w-[400px]">{children}</div>
        </div>
        <p className="px-6 pb-6 text-center text-xs text-fg-subtle">
          Protected by session security and CSRF tokens. Use synthetic data while evaluating.
        </p>
      </main>
    </div>
  );
}
