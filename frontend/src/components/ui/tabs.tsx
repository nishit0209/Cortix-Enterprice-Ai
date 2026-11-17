"use client";

import * as React from "react";
import * as TabsPrimitive from "@radix-ui/react-tabs";
import { motion, useReducedMotion } from "framer-motion";
import { cn } from "@/lib/utils";

const TabsCtx = React.createContext<{ value?: string; id: string }>({ id: "tabs" });

export function Tabs({
  value,
  defaultValue,
  onValueChange,
  className,
  children,
  ...props
}: React.ComponentPropsWithoutRef<typeof TabsPrimitive.Root>) {
  const [inner, setInner] = React.useState(defaultValue);
  const current = value ?? inner;
  const id = React.useId();
  return (
    <TabsCtx.Provider value={{ value: current, id }}>
      <TabsPrimitive.Root
        value={current}
        onValueChange={(v) => {
          setInner(v);
          onValueChange?.(v);
        }}
        className={cn("flex flex-col", className)}
        {...props}
      >
        {children}
      </TabsPrimitive.Root>
    </TabsCtx.Provider>
  );
}

export const TabsList = React.forwardRef<
  React.ComponentRef<typeof TabsPrimitive.List>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.List>
>(({ className, ...props }, ref) => (
  <TabsPrimitive.List
    ref={ref}
    className={cn(
      "scrollbar-thin flex items-center gap-1 overflow-x-auto border-b border-border",
      className,
    )}
    {...props}
  />
));
TabsList.displayName = "TabsList";

export const TabsTrigger = React.forwardRef<
  React.ComponentRef<typeof TabsPrimitive.Trigger>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.Trigger>
>(({ className, children, value, ...props }, ref) => {
  const ctx = React.useContext(TabsCtx);
  const reduce = useReducedMotion();
  const active = ctx.value === value;
  return (
    <TabsPrimitive.Trigger
      ref={ref}
      value={value}
      className={cn(
        "relative -mb-px inline-flex h-10 shrink-0 items-center gap-2 px-3 text-sm font-medium text-fg-muted transition-colors",
        "hover:text-fg focus-ring rounded-t-control data-[state=active]:text-fg [&_svg]:size-4",
        className,
      )}
      {...props}
    >
      {children}
      {active && (
        <motion.span
          layoutId={`${ctx.id}-underline`}
          transition={reduce ? { duration: 0 } : { type: "spring", stiffness: 520, damping: 40 }}
          className="absolute inset-x-2 bottom-0 h-0.5 rounded-full bg-accent"
          aria-hidden
        />
      )}
    </TabsPrimitive.Trigger>
  );
});
TabsTrigger.displayName = "TabsTrigger";

export const TabsContent = React.forwardRef<
  React.ComponentRef<typeof TabsPrimitive.Content>,
  React.ComponentPropsWithoutRef<typeof TabsPrimitive.Content>
>(({ className, ...props }, ref) => (
  <TabsPrimitive.Content ref={ref} className={cn("pt-5 focus-visible:outline-none data-[state=active]:animate-fade", className)} {...props} />
));
TabsContent.displayName = "TabsContent";
