"use client";

import * as React from "react";
import { usePathname, useSearchParams } from "next/navigation";

/**
 * Global slim progress bar displayed at the very top of the window during route transitions.
 * Gives instant (0ms) visual tactile feedback on link click.
 */
export function TopLoader() {
  const pathname = usePathname();
  const searchParams = useSearchParams();
  const [navigating, setNavigating] = React.useState(false);
  const [progress, setProgress] = React.useState(0);

  // When pathname or searchParams change, the new route has mounted.
  React.useEffect(() => {
    if (navigating) {
      setProgress(100);
      const timer = setTimeout(() => {
        setNavigating(false);
        setProgress(0);
      }, 200);
      return () => clearTimeout(timer);
    }
  }, [pathname, searchParams, navigating]);

  // Intercept in-app link clicks immediately on mousedown/click
  React.useEffect(() => {
    const handleNavigationStart = (e: MouseEvent) => {
      // Don't intercept if modifier keys were pressed (open in new tab / window)
      if (e.ctrlKey || e.metaKey || e.shiftKey || e.altKey || e.button !== 0) return;

      const anchor = (e.target as HTMLElement)?.closest("a");
      if (!anchor || !anchor.href) return;
      if (anchor.target && anchor.target !== "_self") return;
      if (anchor.hasAttribute("download")) return;

      try {
        const url = new URL(anchor.href, window.location.href);
        // Only internal links
        if (url.origin === window.location.origin) {
          // Only if actually changing path or query
          const isSamePath = url.pathname === window.location.pathname && url.search === window.location.search;
          if (!isSamePath) {
            setNavigating(true);
            setProgress(30);
          }
        }
      } catch {
        /* invalid url */
      }
    };

    // Use capture phase on document to catch clicks before anything else
    document.addEventListener("click", handleNavigationStart, { capture: true });
    return () => document.removeEventListener("click", handleNavigationStart, { capture: true });
  }, []);

  // Animate progress while navigating
  React.useEffect(() => {
    if (!navigating || progress >= 90) return;
    const interval = setInterval(() => {
      setProgress((prev) => {
        if (prev < 60) return prev + 15;
        if (prev < 80) return prev + 5;
        if (prev < 90) return prev + 2;
        return prev;
      });
    }, 150);
    return () => clearInterval(interval);
  }, [navigating, progress]);

  if (!navigating && progress === 0) return null;

  return (
    <div
      aria-hidden
      className="fixed inset-x-0 top-0 z-[9999] h-[2.5px] pointer-events-none bg-transparent overflow-hidden"
    >
      <div
        className="h-full bg-accent shadow-[0_0_10px_var(--accent)] transition-all ease-out"
        style={{
          width: `${progress}%`,
          opacity: progress === 100 ? 0 : 1,
          transitionDuration: progress === 100 ? "200ms" : "180ms",
        }}
      />
    </div>
  );
}
