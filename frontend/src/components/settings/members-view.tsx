"use client";

import * as React from "react";
import { usePathname, useRouter, useSearchParams } from "next/navigation";
import { useForm, Controller } from "react-hook-form";
import { zodResolver } from "@hookform/resolvers/zod";
import { z } from "zod";
import { Mail, MailX, MoreHorizontal, Send, UserMinus, UserPlus, Users } from "lucide-react";
import { toast } from "sonner";
import { useOrgContext } from "@/components/shell/org-context";
import { PageHeader } from "@/components/ui/page-header";
import { Button } from "@/components/ui/button";
import { Card, CardContent, CardHeader } from "@/components/ui/card";
import { Avatar } from "@/components/ui/avatar";
import { Badge } from "@/components/ui/badge";
import { ConfirmDialog } from "@/components/ui/confirm-dialog";
import { DataTable, type DataTableColumn } from "@/components/ui/data-table";
import { Dialog, DialogBody, DialogContent, DialogFooter, DialogHeader } from "@/components/ui/dialog";
import { EmptyState, Skeleton } from "@/components/ui/feedback";
import { Field, Label } from "@/components/ui/field";
import { Input } from "@/components/ui/input";
import {
  DropdownMenu,
  DropdownMenuContent,
  DropdownMenuItem,
  DropdownMenuTrigger,
  Tooltip,
} from "@/components/ui/overlays";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import { FeatureStatusBadge, StatusBadge } from "@/components/ui/status";
import {
  useInvitations,
  useInviteMember,
  useMembers,
  useRemoveMember,
  useRevokeInvitation,
  useRoles,
  useUpdateMember,
} from "@/lib/queries/org";
import { ROLE_DESCRIPTIONS, ROLE_LABELS } from "@/lib/permissions";
import { errorMessage, isUnavailableEndpoint } from "@/lib/errors";
import { isApiError } from "@/lib/api";
import { formatDate, formatDateTime, formatRelative } from "@/lib/utils";
import type { Member } from "@/lib/types";

const FALLBACK_ROLES = ["owner", "admin", "data_engineer", "analyst", "data_scientist", "viewer", "auditor"];

function useRoleOptions(org: string) {
  const roles = useRoles(org);
  return React.useMemo(() => {
    const list = roles.data?.length
      ? roles.data.map((r) => ({ value: r.key, label: r.name || ROLE_LABELS[r.key] || r.key }))
      : FALLBACK_ROLES.map((k) => ({ value: k, label: ROLE_LABELS[k] }));
    return list;
  }, [roles.data]);
}

export function MembersView() {
  const { slug, me, can } = useOrgContext();
  const router = useRouter();
  const pathname = usePathname();
  const params = useSearchParams();
  const members = useMembers(slug);
  const manage = can("members.manage");
  const invitations = useInvitations(slug, manage);
  const update = useUpdateMember(slug);
  const remove = useRemoveMember(slug);
  const revokeInvite = useRevokeInvitation(slug);
  const roleOptions = useRoleOptions(slug);
  const [inviteOpen, setInviteOpen] = React.useState(false);
  const [removing, setRemoving] = React.useState<Member | null>(null);

  React.useEffect(() => {
    if (params.get("invite") === "1" && manage) {
      setInviteOpen(true);
      router.replace(pathname, { scroll: false });
    }
  }, [params, manage, router, pathname]);

  const changeRole = React.useCallback(
    async (m: Member, role: string) => {
      if (role === m.role) return;
      try {
        await update.mutateAsync({ id: m.id, role });
        toast.success(`${m.user.full_name || m.user.email} is now ${ROLE_LABELS[role] ?? role}`);
      } catch (e) {
        toast.error(
          isApiError(e) && e.status === 409
            ? "An organization must keep at least one owner. Promote someone else first."
            : errorMessage(e),
        );
      }
    },
    [update],
  );

  const columns = React.useMemo<DataTableColumn<Member>[]>(
    () => [
      {
        id: "member",
        accessorFn: (m) => `${m.user.full_name} ${m.user.email}`,
        header: "Member",
        meta: { label: "Member" },
        cell: ({ row }) => {
          const m = row.original;
          const self = m.user.id === me.user.id;
          return (
            <div className="flex min-w-0 items-center gap-3">
              <Avatar name={m.user.full_name || m.user.email} size={32} />
              <div className="min-w-0">
                <div className="flex items-center gap-1.5 truncate font-medium text-fg">
                  {m.user.full_name || m.user.email}
                  {self && <Badge tone="accent">You</Badge>}
                </div>
                <div className="truncate text-xs text-fg-muted">{m.user.email}</div>
              </div>
            </div>
          );
        },
      },
      {
        accessorKey: "role",
        header: "Role",
        cell: ({ row }) => {
          const m = row.original;
          if (!manage) return <span>{ROLE_LABELS[m.role] ?? m.role}</span>;
          return (
            <Select value={m.role} onValueChange={(v) => void changeRole(m, v)}>
              <SelectTrigger className="h-8 w-44" aria-label={`Role for ${m.user.email}`} onClick={(e) => e.stopPropagation()}>
                <SelectValue />
              </SelectTrigger>
              <SelectContent>
                {roleOptions.map((r) => (
                  <SelectItem key={r.value} value={r.value} description={ROLE_DESCRIPTIONS[r.value]}>
                    {r.label}
                  </SelectItem>
                ))}
              </SelectContent>
            </Select>
          );
        },
      },
      {
        accessorKey: "status",
        header: "Status",
        cell: ({ getValue }) => <StatusBadge status={String(getValue())} />,
      },
      {
        accessorKey: "joined_at",
        header: "Joined",
        cell: ({ getValue }) => (
          <Tooltip content={formatDateTime(getValue() as string)}>
            <span className="text-fg-muted">{formatDate(getValue() as string)}</span>
          </Tooltip>
        ),
      },
      ...(manage
        ? [
            {
              id: "actions",
              header: () => <span className="sr-only">Actions</span>,
              enableSorting: false,
              enableHiding: false,
              meta: { align: "right" as const, hideOnMobile: false },
              cell: ({ row }: { row: { original: Member } }) => (
                <DropdownMenu>
                  <DropdownMenuTrigger asChild>
                    <Button variant="ghost" size="icon-xs" aria-label={`Actions for ${row.original.user.email}`}>
                      <MoreHorizontal />
                    </Button>
                  </DropdownMenuTrigger>
                  <DropdownMenuContent align="end">
                    <DropdownMenuItem destructive onSelect={() => setRemoving(row.original)}>
                      <UserMinus /> {row.original.user.id === me.user.id ? "Leave organization" : "Remove from organization"}
                    </DropdownMenuItem>
                  </DropdownMenuContent>
                </DropdownMenu>
              ),
            } satisfies DataTableColumn<Member>,
          ]
        : []),
    ],
    [manage, me.user.id, roleOptions, changeRole],
  );

  const pending = (invitations.data ?? []).filter((i) => i.status === "pending" || i.status === "sent");
  const unavailable = members.isError && isUnavailableEndpoint(members.error);

  return (
    <div className="space-y-6">
      <PageHeader
        title="Members"
        badge={<FeatureStatusBadge status="working" />}
        description="People with access to this organization and their roles."
        actions={
          manage ? (
            <Button onClick={() => setInviteOpen(true)}>
              <UserPlus /> Invite member
            </Button>
          ) : undefined
        }
      />

      <DataTable
        caption="Organization members"
        columns={columns}
        data={members.data}
        loading={members.isLoading}
        error={members.isError && !unavailable ? members.error : undefined}
        onRetry={() => void members.refetch()}
        searchPlaceholder="Search by name or email…"
        getRowId={(m) => m.id}
        filters={ROLE_FILTER}
        emptyState={
          <EmptyState
            icon={<Users />}
            title={unavailable ? "Member service unavailable" : "No members yet"}
            description={unavailable ? "Members appear once the organization service responds." : "Invite teammates to collaborate."}
          />
        }
      />

      {manage && (
        <Card>
          <CardHeader
            icon={<Mail />}
            title="Pending invitations"
            description="Invitations expire automatically. Revoke one to invalidate its link."
          />
          <CardContent>
            {invitations.isLoading ? (
              <div className="space-y-2">
                <Skeleton className="h-10 w-full" />
                <Skeleton className="h-10 w-full" />
              </div>
            ) : invitations.isError ? (
              <p className="text-sm text-fg-muted">Invitations couldn&apos;t be loaded.</p>
            ) : pending.length === 0 ? (
              <EmptyState compact icon={<Send />} title="No pending invitations" />
            ) : (
              <ul className="divide-y divide-border rounded-control border border-border">
                {pending.map((inv) => (
                  <li key={inv.id} className="flex flex-wrap items-center gap-3 px-4 py-3">
                    <Avatar name={inv.email} size={28} />
                    <div className="min-w-0 flex-1">
                      <div className="truncate text-sm font-medium text-fg">{inv.email}</div>
                      <div className="text-xs text-fg-muted">
                        {ROLE_LABELS[inv.role] ?? inv.role} · expires {formatRelative(inv.expires_at)}
                      </div>
                    </div>
                    <Button
                      variant="ghost"
                      size="sm"
                      loading={revokeInvite.isPending && revokeInvite.variables === inv.id}
                      onClick={async () => {
                        try {
                          await revokeInvite.mutateAsync(inv.id);
                          toast.success(`Invitation to ${inv.email} revoked`);
                        } catch (e) {
                          toast.error(errorMessage(e));
                        }
                      }}
                    >
                      <MailX /> Revoke
                    </Button>
                  </li>
                ))}
              </ul>
            )}
          </CardContent>
        </Card>
      )}

      <InviteDialog org={slug} open={inviteOpen} onOpenChange={setInviteOpen} roleOptions={roleOptions} />
      <ConfirmDialog
        open={!!removing}
        onOpenChange={(o) => !o && setRemoving(null)}
        title={removing?.user.id === me.user.id ? "Leave this organization?" : `Remove ${removing?.user.full_name || removing?.user.email}?`}
        description="They lose access to all data, dashboards and settings in this organization immediately. You can invite them again later."
        confirmLabel={removing?.user.id === me.user.id ? "Leave organization" : "Remove member"}
        loading={remove.isPending}
        onConfirm={async () => {
          if (!removing) return;
          try {
            await remove.mutateAsync(removing.id);
            toast.success("Member removed");
            setRemoving(null);
          } catch (e) {
            toast.error(
              isApiError(e) && e.status === 409
                ? "You can't remove the last owner. Promote another member to owner first."
                : errorMessage(e),
            );
          }
        }}
      />
    </div>
  );
}

const ROLE_FILTER = [
  {
    columnId: "role",
    label: "Role",
    options: FALLBACK_ROLES.map((k) => ({ value: k, label: ROLE_LABELS[k] })),
  },
];

const inviteSchema = z.object({
  email: z.string().trim().email("Enter a valid email address"),
  role: z.string().min(1, "Choose a role"),
});

function InviteDialog({
  org,
  open,
  onOpenChange,
  roleOptions,
}: {
  org: string;
  open: boolean;
  onOpenChange: (o: boolean) => void;
  roleOptions: Array<{ value: string; label: string }>;
}) {
  const invite = useInviteMember(org);
  const form = useForm<z.infer<typeof inviteSchema>>({
    resolver: zodResolver(inviteSchema),
    defaultValues: { email: "", role: "analyst" },
  });

  React.useEffect(() => {
    if (!open) form.reset({ email: "", role: "analyst" });
  }, [open, form]);

  const onSubmit = form.handleSubmit(async (values) => {
    try {
      await invite.mutateAsync(values);
      toast.success(`Invitation sent to ${values.email}`);
      onOpenChange(false);
    } catch (e) {
      if (isApiError(e) && e.errors.length) {
        for (const fe of e.errors) {
          if (fe.field === "email" || fe.field === "role") form.setError(fe.field, { message: fe.message });
        }
        if (!e.errors.some((fe) => fe.field === "email" || fe.field === "role")) toast.error(errorMessage(e));
      } else toast.error(errorMessage(e));
    }
  });

  return (
    <Dialog open={open} onOpenChange={onOpenChange}>
      <DialogContent size="sm">
        <form onSubmit={onSubmit} noValidate>
          <DialogHeader icon={<UserPlus />} title="Invite a teammate" description="They'll get an email with a link to join." />
          <DialogBody className="space-y-4">
            <Field id="invite-email" label="Email" error={form.formState.errors.email?.message}>
              <Input type="email" placeholder="teammate@company.com" autoFocus {...form.register("email")} />
            </Field>
            <div className="space-y-1.5">
              <Label htmlFor="invite-role">Role</Label>
              <Controller
                control={form.control}
                name="role"
                render={({ field }) => (
                  <Select value={field.value} onValueChange={field.onChange}>
                    <SelectTrigger id="invite-role">
                      <SelectValue />
                    </SelectTrigger>
                    <SelectContent>
                      {roleOptions.map((r) => (
                        <SelectItem key={r.value} value={r.value} description={ROLE_DESCRIPTIONS[r.value]}>
                          {r.label}
                        </SelectItem>
                      ))}
                    </SelectContent>
                  </Select>
                )}
              />
              {form.formState.errors.role && <p className="text-xs text-danger">{form.formState.errors.role.message}</p>}
            </div>
          </DialogBody>
          <DialogFooter>
            <Button type="button" variant="secondary" onClick={() => onOpenChange(false)}>
              Cancel
            </Button>
            <Button type="submit" loading={invite.isPending}>
              <Send /> Send invitation
            </Button>
          </DialogFooter>
        </form>
      </DialogContent>
    </Dialog>
  );
}
