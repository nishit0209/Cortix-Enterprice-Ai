import { OrgGate } from "@/components/shell/org-gate";

export default function OrgLayout({ children }: { children: React.ReactNode }) {
  return <OrgGate>{children}</OrgGate>;
}
