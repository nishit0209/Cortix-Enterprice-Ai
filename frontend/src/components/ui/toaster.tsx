"use client";

import { Toaster as Sonner } from "sonner";
import { useTheme } from "next-themes";

export function Toaster() {
  const { resolvedTheme } = useTheme();
  return (
    <Sonner
      theme={resolvedTheme === "dark" ? "dark" : "light"}
      position="bottom-right"
      closeButton
      toastOptions={{
        classNames: {
          toast:
            "!rounded-card !border !border-border !bg-surface !text-fg !shadow-pop !font-sans !text-sm",
          description: "!text-fg-muted",
          actionButton: "!bg-accent !text-accent-fg",
          cancelButton: "!bg-surface-2 !text-fg",
          closeButton: "!bg-surface !border-border !text-fg-muted",
        },
      }}
    />
  );
}

export { toast } from "sonner";
