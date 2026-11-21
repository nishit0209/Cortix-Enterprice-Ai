"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { useTheme } from "next-themes";
import { Keyboard, LogOut, Monitor, Moon, Settings, ShieldHalf, Sun } from "lucide-react";
import { toast } from "sonner";
import { Avatar } from "@/components/ui/avatar";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuRadioGroup,
  DropdownMenuRadioItem,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/overlays";
import { Kbd } from "@/components/ui/feedback";
import { useLogout } from "@/lib/queries/auth";
import { ROLE_LABELS } from "@/lib/permissions";
import { useOrgContext } from "./org-context";

export function useSignOut() {
  const router = useRouter();
  const logout = useLogout();
  return React.useCallback(async () => {
    try {
      await logout.mutateAsync();
    } catch {
      // Even if the server call fails, leave the app; the session will expire server-side.
    }
    toast.success("Signed out");
    router.replace("/login");
  }, [logout, router]);
}

export function UserMenu({ onOpenCommand }: { onOpenCommand: () => void }) {
  const { me, summary, slug } = useOrgContext();
  const { theme, setTheme } = useTheme();
  const router = useRouter();
  const signOut = useSignOut();
  const name = me.user.full_name || me.user.email;

  return (
    <DropdownMenu>
      <DropdownMenuTrigger asChild>
        <button type="button" className="rounded-full focus-ring" aria-label={`Account menu for ${name}`}>
          <Avatar name={name} size={30} />
        </button>
      </DropdownMenuTrigger>
      <DropdownMenuContent align="end" className="w-64">
        <div className="flex items-center gap-3 px-2 py-2">
          <Avatar name={name} size={34} />
          <div className="min-w-0">
            <div className="truncate text-sm font-medium text-fg">{me.user.full_name || "Unnamed user"}</div>
            <div className="truncate text-xs text-fg-muted">{me.user.email}</div>
          </div>
        </div>
        {summary && (
          <div className="px-2 pb-2 text-xs text-fg-subtle">
            {ROLE_LABELS[summary.role] ?? summary.role} in <span className="text-fg-muted">{summary.name}</span>
          </div>
        )}
        <DropdownMenuSeparator />
        <DropdownMenuLabel>Theme</DropdownMenuLabel>
        <DropdownMenuRadioGroup value={theme ?? "system"} onValueChange={setTheme}>
          <DropdownMenuRadioItem value="system">
            <Monitor /> System
          </DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="light">
            <Sun /> Light
          </DropdownMenuRadioItem>
          <DropdownMenuRadioItem value="dark">
            <Moon /> Dark
          </DropdownMenuRadioItem>
        </DropdownMenuRadioGroup>
        <DropdownMenuSeparator />
        <DropdownMenuItem onSelect={() => router.push(`/o/${slug}/settings`)}>
          <Settings /> Organization settings
        </DropdownMenuItem>
        {me.user.is_platform_admin && (
          <DropdownMenuItem onSelect={() => router.push("/platform")}>
            <ShieldHalf /> Platform console
          </DropdownMenuItem>
        )}
        <DropdownMenuItem onSelect={onOpenCommand} shortcut={<Kbd>⌘K</Kbd>}>
          <Keyboard /> Command menu
        </DropdownMenuItem>
        <DropdownMenuSeparator />
        <DropdownMenuItem destructive onSelect={() => void signOut()}>
          <LogOut /> Sign out
        </DropdownMenuItem>
      </DropdownMenuContent>
    </DropdownMenu>
  );
}
