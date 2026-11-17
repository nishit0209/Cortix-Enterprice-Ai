import { redirect } from "next/navigation";

export default async function DataIndex({ params }: { params: Promise<{ org: string }> }) {
  const { org } = await params;
  redirect(`/o/${org}/data/datasets`);
}
