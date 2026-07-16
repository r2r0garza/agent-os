const DEFAULT_API_URL = "http://127.0.0.1:8000/api/v1"

async function proxy(
  request: Request,
  context: { params: Promise<{ path: string[] }> }
) {
  const { path } = await context.params
  const upstreamBase = (
    process.env.AGENTIC_OS_API_URL ?? DEFAULT_API_URL
  ).replace(/\/$/, "")
  const incomingUrl = new URL(request.url)
  const upstreamUrl = `${upstreamBase}/${path.join("/")}${incomingUrl.search}`
  const actorUserId =
    request.headers.get("x-agentic-user-id") ??
    process.env.AGENTIC_OS_USER_ID ??
    null

  try {
    const upstream = await fetch(upstreamUrl, {
      method: request.method,
      body:
        request.method === "GET" || request.method === "HEAD"
          ? undefined
          : await request.arrayBuffer(),
      cache: "no-store",
      headers: {
        accept: request.headers.get("accept") ?? "application/json",
        "content-type":
          request.headers.get("content-type") ?? "application/json",
        ...(actorUserId ? { "x-agentic-user-id": actorUserId } : {}),
      },
      signal: AbortSignal.timeout(15_000),
    })

    return new Response(upstream.body, {
      status: upstream.status,
      headers: {
        "content-type":
          upstream.headers.get("content-type") ?? "application/json",
      },
    })
  } catch {
    return Response.json(
      {
        error:
          "The Agentic OS API is unavailable. Start the backend and retry this request.",
      },
      { status: 502 }
    )
  }
}

export const GET = proxy
export const POST = proxy
