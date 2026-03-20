import RunDetailClient from "./RunDetailClient";

export function generateStaticParams() {
  return [];
}

export default async function RunDetailPage({
  params,
}: {
  params: Promise<{ id: string }>;
}) {
  const { id } = await params;
  return <RunDetailClient runId={id} />;
}
