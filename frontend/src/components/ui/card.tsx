import * as React from "react";
import { cn } from "@/lib/utils";

export const Card = React.forwardRef<HTMLDivElement, React.HTMLAttributes<HTMLDivElement> & { interactive?: boolean }>(
  ({ className, interactive, ...props }, ref) => (
    <div
      ref={ref}
      className={cn(
        "rounded-card border border-border bg-surface shadow-xs",
        interactive && "transition-[border-color,box-shadow] duration-150 hover:border-border-strong hover:shadow-soft",
        className,
      )}
      {...props}
    />
  ),
);
Card.displayName = "Card";

export function CardHeader({
  className,
  title,
  description,
  actions,
  icon,
  children,
}: {
  className?: string;
  title?: React.ReactNode;
  description?: React.ReactNode;
  actions?: React.ReactNode;
  icon?: React.ReactNode;
  children?: React.ReactNode;
}) {
  return (
    <div className={cn("flex items-start justify-between gap-3 px-5 pt-4 pb-3", className)}>
      <div className="flex min-w-0 items-start gap-3">
        {icon && (
          <span className="mt-0.5 flex size-7 shrink-0 items-center justify-center rounded-control border border-border bg-surface-2 text-fg-muted [&_svg]:size-4">
            {icon}
          </span>
        )}
        <div className="min-w-0">
          {title && <h3 className="truncate text-sm font-semibold text-fg">{title}</h3>}
          {description && <p className="mt-0.5 text-xs text-fg-muted">{description}</p>}
          {children}
        </div>
      </div>
      {actions && <div className="flex shrink-0 items-center gap-1.5">{actions}</div>}
    </div>
  );
}

export function CardContent({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return <div className={cn("px-5 pb-5", className)} {...props} />;
}

export function CardFooter({ className, ...props }: React.HTMLAttributes<HTMLDivElement>) {
  return (
    <div
      className={cn("flex items-center justify-between gap-3 border-t border-border px-5 py-3 text-xs text-fg-muted", className)}
      {...props}
    />
  );
}
