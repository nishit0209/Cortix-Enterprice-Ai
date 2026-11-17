import Link from "next/link";
import { Compass } from "lucide-react";
import { Logo } from "@/components/brand/logo";
import { Button } from "@/components/ui/button";

export default function NotFound() {
  return (
    <div className="flex min-h-dvh flex-col items-center justify-center gap-8 bg-bg px-4 text-center">
      <Logo />
      <div className="space-y-3">
        <span className="mx-auto flex size-12 items-center justify-center rounded-card border border-border bg-surface shadow-soft">
          <Compass className="size-5 text-fg-muted" aria-hidden />
        </span>
        <h1 className="text-xl font-semibold tracking-[-0.015em] text-fg">Page not found</h1>
        <p className="max-w-sm text-sm text-fg-muted">The page you&apos;re looking for doesn&apos;t exist or has moved.</p>
      </div>
      <Button asChild>
        <Link href="/">Go to your workspace</Link>
      </Button>
    </div>
  );
}
