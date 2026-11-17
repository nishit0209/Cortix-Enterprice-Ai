import { Suspense } from "react";
import { MembersView } from "@/components/settings/members-view";

export default function MembersPage() {
  return (
    <Suspense>
      <MembersView />
    </Suspense>
  );
}
