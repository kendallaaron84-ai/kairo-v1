import { resolveKairoApiUrl } from '@/lib/kairo-api';

export const dynamic = 'force-dynamic';

export async function GET(
  _request: Request,
  { params }: { params: Promise<{ path: string[] }> },
): Promise<Response> {
  try {
    const { path } = await params;
    const upstream = await fetch(resolveKairoApiUrl(path.join('/')), {
      cache: 'no-store',
      headers: { accept: 'application/json' },
      signal: AbortSignal.timeout(5_000),
    });
    return new Response(await upstream.arrayBuffer(), {
      status: upstream.status,
      headers: { 'content-type': upstream.headers.get('content-type') ?? 'application/json' },
    });
  } catch {
    return Response.json(
      { detail: 'Kairo API is unavailable' },
      { status: 502 },
    );
  }
}
