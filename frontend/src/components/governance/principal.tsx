"use client";

import * as React from "react";
import { Select, SelectContent, SelectItem, SelectTrigger, SelectValue } from "@/components/ui/select";
import type { Member, Role } from "@/lib/types";
import type { PrincipalType } from "@/lib/queries/governance";

export interface Principal {
  type: PrincipalType;
  id: string | null;
}

export function principalLabel(p: { principal_type: string; principal_id: string | null }, roles: Role[], members: Member[]): string {
  if (p.principal_type === "everyone") return "Everyone";
  if (p.principal_type === "role") return `Role: ${roles.find((r) => r.id === p.principal_id)?.name ?? "unknown role"}`;
  const m = members.find((x) => x.user.id === p.principal_id);
  return m ? (m.user.full_name ? `${m.user.full_name} (${m.user.email})` : m.user.email) : "Unknown member";
}

/** Principal picker: type (everyone/role/user) + the role or member. */
export function PrincipalPicker({
  value,
  onChange,
  roles,
  members,
  allowEveryone,
  idPrefix,
}: {
  value: Principal;
  onChange: (p: Principal) => void;
  roles: Role[];
  members: Member[];
  allowEveryone: boolean;
  idPrefix: string;
}) {
  return (
    <div className="grid gap-2 sm:grid-cols-[140px_minmax(0,1fr)]">
      <Select value={value.type} onValueChange={(t) => onChange({ type: t as PrincipalType, id: null })}>
        <SelectTrigger aria-label="Applies to" id={`${idPrefix}-ptype`}>
          <SelectValue />
        </SelectTrigger>
        <SelectContent>
          {allowEveryone && <SelectItem value="everyone">Everyone</SelectItem>}
          <SelectItem value="role">A role</SelectItem>
          <SelectItem value="user">A member</SelectItem>
        </SelectContent>
      </Select>
      {value.type === "role" && (
        <Select value={value.id ?? undefined} onValueChange={(id) => onChange({ type: "role", id })}>
          <SelectTrigger aria-label="Role">
            <SelectValue placeholder="Choose a role" />
          </SelectTrigger>
          <SelectContent>
            {roles.map((r) => (
              <SelectItem key={r.id} value={r.id}>
                {r.name}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
      {value.type === "user" && (
        <Select value={value.id ?? undefined} onValueChange={(id) => onChange({ type: "user", id })}>
          <SelectTrigger aria-label="Member">
            <SelectValue placeholder="Choose a member" />
          </SelectTrigger>
          <SelectContent>
            {members.map((m) => (
              <SelectItem key={m.user.id} value={m.user.id} description={m.user.email}>
                {m.user.full_name || m.user.email}
              </SelectItem>
            ))}
          </SelectContent>
        </Select>
      )}
    </div>
  );
}

export const principalComplete = (p: Principal) => p.type === "everyone" || !!p.id;
