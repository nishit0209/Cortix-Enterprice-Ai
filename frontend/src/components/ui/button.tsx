import * as React from "react";
import { Slot } from "@radix-ui/react-slot";
import { cva, type VariantProps } from "class-variance-authority";
import { Loader2 } from "lucide-react";
import { cn } from "@/lib/utils";

export const buttonVariants = cva(
  [
    "relative inline-flex select-none items-center justify-center gap-2 whitespace-nowrap font-medium",
    "rounded-control transition-[background-color,border-color,color,box-shadow,transform] duration-150 ease-out",
    "focus-ring disabled:pointer-events-none disabled:opacity-50 active:translate-y-px",
    "[&_svg]:pointer-events-none [&_svg]:shrink-0",
  ],
  {
    variants: {
      variant: {
        primary:
          "bg-accent text-accent-fg shadow-[inset_0_1px_0_rgb(255_255_255/0.14),0_1px_2px_rgb(14_17_22/0.12)] hover:bg-accent-hover",
        secondary: "border border-border bg-surface text-fg shadow-xs hover:border-border-strong hover:bg-surface-2",
        ghost: "text-fg-muted hover:bg-surface-2 hover:text-fg",
        soft: "bg-accent-soft text-accent-soft-fg hover:brightness-[0.97] dark:hover:brightness-125",
        danger: "bg-danger text-white shadow-xs hover:brightness-110 dark:text-[#0b0c0f]",
        "danger-ghost": "text-danger hover:bg-danger-soft",
        link: "h-auto px-0 text-accent underline-offset-4 hover:underline",
      },
      size: {
        xs: "h-7 px-2 text-xs [&_svg]:size-3.5",
        sm: "h-8 px-3 text-sm [&_svg]:size-4",
        md: "h-9 px-3.5 text-sm [&_svg]:size-4",
        lg: "h-11 px-5 text-base [&_svg]:size-4",
        icon: "size-9 [&_svg]:size-4",
        "icon-sm": "size-8 [&_svg]:size-4",
        "icon-xs": "size-7 [&_svg]:size-3.5",
      },
    },
    defaultVariants: { variant: "primary", size: "md" },
  },
);

export interface ButtonProps
  extends React.ButtonHTMLAttributes<HTMLButtonElement>,
    VariantProps<typeof buttonVariants> {
  asChild?: boolean;
  loading?: boolean;
}

export const Button = React.forwardRef<HTMLButtonElement, ButtonProps>(
  ({ className, variant, size, asChild = false, loading = false, disabled, children, ...props }, ref) => {
    const Comp = asChild ? Slot : "button";
    return (
      <Comp
        ref={ref}
        className={cn(buttonVariants({ variant, size }), className)}
        disabled={asChild ? undefined : disabled || loading}
        aria-busy={loading || undefined}
        {...props}
      >
        {asChild ? (
          children
        ) : (
          <>
            {loading && <Loader2 className="animate-spin" aria-hidden />}
            {children}
          </>
        )}
      </Comp>
    );
  },
);
Button.displayName = "Button";
