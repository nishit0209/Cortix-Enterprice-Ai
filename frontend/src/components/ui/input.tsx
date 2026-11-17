import * as React from "react";
import { cn } from "@/lib/utils";

export const controlBase = cn(
  "w-full rounded-control border border-border bg-surface text-fg shadow-xs",
  "placeholder:text-fg-subtle transition-[border-color,box-shadow] duration-150",
  "hover:border-border-strong focus:outline-none focus:border-accent focus:ring-3 focus:ring-accent/15",
  "disabled:cursor-not-allowed disabled:opacity-60",
  "aria-[invalid=true]:border-danger aria-[invalid=true]:focus:ring-danger/15",
);

export interface InputProps extends React.InputHTMLAttributes<HTMLInputElement> {
  leading?: React.ReactNode;
  trailing?: React.ReactNode;
}

export const Input = React.forwardRef<HTMLInputElement, InputProps>(
  ({ className, leading, trailing, type = "text", ...props }, ref) => {
    if (!leading && !trailing) {
      return <input ref={ref} type={type} className={cn(controlBase, "h-9 px-3 text-sm", className)} {...props} />;
    }
    return (
      <div className={cn("relative flex items-center", className)}>
        {leading && (
          <span className="pointer-events-none absolute left-3 flex text-fg-subtle [&_svg]:size-4">{leading}</span>
        )}
        <input
          ref={ref}
          type={type}
          className={cn(controlBase, "h-9 text-sm", leading ? "pl-9" : "pl-3", trailing ? "pr-10" : "pr-3")}
          {...props}
        />
        {trailing && <span className="absolute right-2 flex items-center text-fg-subtle">{trailing}</span>}
      </div>
    );
  },
);
Input.displayName = "Input";

export const Textarea = React.forwardRef<HTMLTextAreaElement, React.TextareaHTMLAttributes<HTMLTextAreaElement>>(
  ({ className, ...props }, ref) => (
    <textarea ref={ref} className={cn(controlBase, "min-h-20 px-3 py-2 text-sm leading-5", className)} {...props} />
  ),
);
Textarea.displayName = "Textarea";
