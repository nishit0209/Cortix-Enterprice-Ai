import type { Metadata } from "next";
import { PlatformConsole } from "@/components/platform/platform-console";

export const metadata: Metadata = { title: "Platform console", robots: { index: false, follow: false } };

/** Platform super-admin console. Renders the regular 404 page unless `/me` says `is_platform_admin`. */
export default function PlatformPage() {
  return <PlatformConsole />;
}
