"use client";

import * as React from "react";
import * as DialogPrimitive from "@radix-ui/react-dialog";
import { X } from "lucide-react";
import { cn } from "@/lib/utils";

export const Dialog = DialogPrimitive.Root;
export const DialogTrigger = DialogPrimitive.Trigger;
export const DialogClose = DialogPrimitive.Close;

export const DialogContent = React.forwardRef<
  React.ComponentRef<typeof DialogPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof DialogPrimitive.Content> & {
    size?: "sm" | "md" | "lg" | "xl";
    hideClose?: boolean;
  }
>(({ className, children, size = "md", hideClose, ...props }, ref) => (
  <DialogPrimitive.Portal>
    <DialogPrimitive.Overlay className="fixed inset-0 z-50 bg-overlay backdrop-blur-[2px] data-[state=open]:animate-fade" />
    <DialogPrimitive.Content
      ref={ref}
      className={cn(
        "fixed left-1/2 top-1/2 z-50 flex max-h-[calc(100dvh-32px)] w-[calc(100vw-32px)] -translate-x-1/2 -translate-y-1/2 flex-col",
        "rounded-modal border border-border bg-surface text-fg shadow-pop focus:outline-none data-[state=open]:animate-pop",
        size === "sm" && "max-w-sm",
        size === "md" && "max-w-lg",
        size === "lg" && "max-w-2xl",
        size === "xl" && "max-w-4xl",
        className,
      )}
      {...props}
    >
      {children}
      {!hideClose && (
        <DialogPrimitive.Close
          className="absolute right-3 top-3 flex size-8 items-center justify-center rounded-control text-fg-subtle transition-colors hover:bg-surface-2 hover:text-fg focus-ring"
          aria-label="Close"
        >
          <X className="size-4" />
        </DialogPrimitive.Close>
      )}
    </DialogPrimitive.Content>
  </DialogPrimitive.Portal>
));
DialogContent.displayName = "DialogContent";

export function DialogHeader({
  title,
  description,
  icon,
  className,
}: {
  title: React.ReactNode;
  description?: React.ReactNode;
  icon?: React.ReactNode;
  className?: string;
}) {
  return (
    <div className={cn("flex gap-3 px-6 pt-6 pb-2 pr-12", className)}>
      {icon && (
        <span className="flex size-9 shrink-0 items-center justify-center rounded-card border border-border bg-surface-2 text-fg-muted [&_svg]:size-4.5">
          {icon}
        </span>
      )}
      <div className="min-w-0">
        <DialogPrimitive.Title className="text-md font-semibold tracking-[-0.01em] text-fg">{title}</DialogPrimitive.Title>
        {description ? (
          <DialogPrimitive.Description className="mt-1 text-sm text-fg-muted">{description}</DialogPrimitive.Description>
        ) : (
          <DialogPrimitive.Description className="sr-only">{typeof title === "string" ? title : "Dialog"}</DialogPrimitive.Description>
        )}
      </div>
    </div>
  );
}

export function DialogBody({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("scrollbar-thin min-h-0 flex-1 overflow-y-auto px-6 py-4", className)} {...props} />;
}

export function DialogFooter({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn(
        "flex flex-col-reverse gap-2 border-t border-border bg-surface-2/60 px-6 py-3.5 sm:flex-row sm:items-center sm:justify-end rounded-b-modal",
        className,
      )}
      {...props}
    />
  );
}
