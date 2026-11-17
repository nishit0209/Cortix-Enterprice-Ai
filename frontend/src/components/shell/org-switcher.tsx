"use client";

import * as React from "react";
import { useRouter } from "next/navigation";
import { useForm } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { Building2, Check, ChevronsUpDown, Plus } from "lucide-react";
import { toast } from "sonner";
import { Avatar } from "@/components/ui/avatar";
import { Button } from "@/components/ui/button";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader } from "@/components/ui/dialog";
import { Field } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuLabel,
  DropdownMenuSeparator,
  DropdownMenuTrigger,
} from "@/components/ui/overlays";
import { useCreateOrg } from "@/lib/queries/auth";
import { ROLE_LABELS } from "@/lib/permissions";
import { isApiError } from "@/lib/api";
import { errorMessage } from "@/lib/errors";
import { useOrgContext } from "./org-context";

export function OrgSwitcher() {
  const { me, slug, summary, org } = useOrgContext();
  const router = useRouter();
  const [createOpen, setCreateOpen] = React.useState(false);
  const name = org?.name ?? summary?.name ?? slug;

  return (
    <>
      <DropdownMenu>
        <DropdownMenuTrigger asChild>
          <button
            type="button"
            className="flex h-8 min-w-0 max-w-[220px] items-center gap-2 rounded-control px-1.5 text-sm font-medium text-fg transition-colors hover:bg-surface-2 focus-ring data-[state=open]:bg-surface-2"
            aria-label={`Organization: ${name}. Switch organization`}
          >
            <Avatar name={name} size={22} square />
            <span className="truncate">{name}</span>
            <ChevronsUpDown className="size-3.5 shrink-0 text-fg-subtle" aria-hidden />
          </button>
        </DropdownMenuTrigger>
        <DropdownMenuContent align="start" className="w-64">
          <DropdownMenuLabel>Organizations</DropdownMenuLabel>
          {me.orgs.map((o) => (
            <DropdownMenuItem key={o.id} onSelect={() => router.push(`/o/${o.slug}/dashboard`)} className="gap-2.5">
              <Avatar name={o.name} size={22} square />
              <span className="min-w-0 flex-1">
                <span className="block truncate text-fg">{o.name}</span>
                <span className="block truncate text-xs text-fg-subtle">{ROLE_LABELS[o.role] ?? o.role}</span>
              </span>
              {o.slug === slug && <Check className="!text-accent" aria-label="Current" />}
            </DropdownMenuItem>
          ))}
          <DropdownMenuSeparator />
          <DropdownMenuItem onSelect={() => setCreateOpen(true)}>
            <Plus /> Create organization
          </DropdownMenuItem>
        </DropdownMenuContent>
      </DropdownMenu>
      <CreateOrgDialog open={createOpen} onOpenChange={setCreateOpen} />
    </>
  );
}

const schema = z.object({
  name: z.string().trim().min(2, "Use at least 2 characters").max(120, "Use at most 120 characters"),
});

export function CreateOrgDialog({ open, onOpenChange }: { open: boolean; onOpenChange: (o: boolean) => void }) {
  const router = useRouter();
  const create = useCreateOrg();
  const form = useForm<z.infer<typeof schema>>({ resolver: zodResolver(schema), defaultValues: { name: "" } });

  React.useEffect(() => {
    if (!open) form.reset();
  }, [open, form]);

  const onSubmit = form.handleSubmit(async (values) => {
    try {
      const org = await create.mutateAsync(values);
      toast.success(`${org.name} created`, { description: "New organizations start with the synthetic_only AI policy." });
      onOpenChange(false);
      router.push(`/o/${org.slug}/dashboard`);
    } catch (e) {
      if (isApiError(e) && e.errors.length) form.setError("name", { message: e.errors[0].message });
      else toast.error(errorMessage(e));
    }
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="sm">
        <form onSubmit={onSubmit} noValidate>
          <DialogHeader
            icon={<Building2 />}
            title="Create organization"
            description="A separate, isolated workspace with its own members, data and AI policy."
          />
          <DialogBody>
            <Field id="org-name" label="Organization name" error={form.formState.errors.name?.message}>
              <Input placeholder="Acme Analytics" autoFocus {...form.register("name")} />
            </Field>
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={create.isPending}>
              Create organization
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
