/**
 * The production server for the operator console.
 *
 * A deliberately small Node HTTP server. It does two things:
 *
 * 1. Serves the built assets from `dist/`.
 * 2. Proxies `/api/*` to the backend.
 *
 * The proxy is the reason the frontend needs **no CORS configuration**: the
 * browser only ever talks to this origin, and this origin talks to the
 * backend. It also means the browser never learns the backend's internal
 * hostname.
 *
 * SSE is proxied as a raw byte stream — no buffering, no compression — so the
 * event stream arrives as it happens rather than in a batch at the end.
 */

import { createServer } from 'node:http'
import { request as httpRequest } from 'node:http'
import { request as httpsRequest } from 'node:https'
import { createReadStream, existsSync, statSync } from 'node:fs'
import { extname, join, normalize, resolve } from 'node:path'
import { fileURLToPath } from 'node:url'

const HERE = fileURLToPath(new URL('.', import.meta.url))
const DIST = resolve(HERE, 'dist')
const PORT = Number(process.env.PORT ?? 8003)
const BACKEND = process.env.BACKEND_ORIGIN ?? 'http://backend:8002'

const MIME = {
  '.html': 'text/html; charset=utf-8',
  '.js': 'text/javascript; charset=utf-8',
  '.css': 'text/css; charset=utf-8',
  '.json': 'application/json; charset=utf-8',
  '.svg': 'image/svg+xml',
  '.png': 'image/png',
  '.jpg': 'image/jpeg',
  '.ico': 'image/x-icon',
  '.map': 'application/json; charset=utf-8',
  '.woff2': 'font/woff2',
}

/** Headers we must not copy back from the upstream response. */
const STRIPPED = new Set([
  'connection',
  'keep-alive',
  'transfer-encoding',
  'content-encoding',
  'content-length',
])

function proxy(req, res) {
  const isHttps = BACKEND.startsWith('https:')
  const forward = isHttps ? httpsRequest : httpRequest

  const upstream = forward(
    BACKEND,
    {
      method: req.method,
      path: req.url,
      headers: {
        ...req.headers,
        host: new URL(BACKEND).host,
        'accept-encoding': 'identity',
      },
    },
    (upstreamRes) => {
      const headers = {}
      for (const [key, value] of Object.entries(upstreamRes.headers)) {
        if (!STRIPPED.has(key.toLowerCase())) headers[key] = value
      }
      res.writeHead(upstreamRes.statusCode ?? 502, headers)

      // Stream the body through untouched. For SSE this is what keeps the
      // events arriving live rather than in one lump at the end.
      upstreamRes.pipe(res)
    },
  )

  upstream.on('error', (err) => {
    if (res.headersSent) {
      res.end()
      return
    }
    res.writeHead(502, { 'Content-Type': 'application/json; charset=utf-8' })
    res.end(
      JSON.stringify({
        status: 'ERROR',
        error: {
          code: 'UPSTREAM_UNAVAILABLE',
          message: `Could not reach the backend: ${err.message}`,
          http_status: 502,
        },
      }),
    )
  })

  req.pipe(upstream)
}

function serveStatic(req, res) {
  const url = new URL(req.url ?? '/', 'http://localhost')
  let pathname = decodeURIComponent(url.pathname)

  if (pathname === '/') pathname = '/index.html'

  // Contain the path inside dist/ — a `..` in the URL must not escape it.
  const candidate = resolve(join(DIST, normalize(pathname)))
  if (!candidate.startsWith(DIST)) {
    res.writeHead(403, { 'Content-Type': 'text/plain; charset=utf-8' })
    res.end('Forbidden')
    return
  }

  let filePath = candidate
  if (!existsSync(filePath) || !statSync(filePath).isFile()) {
    // A single-page app: any unknown path falls back to the shell, and the
    // client-side router takes it from there.
    filePath = join(DIST, 'index.html')
  }

  if (!existsSync(filePath)) {
    res.writeHead(503, { 'Content-Type': 'text/plain; charset=utf-8' })
    res.end('The frontend has not been built. Run `npm run build` first.')
    return
  }

  const type = MIME[extname(filePath)] ?? 'application/octet-stream'
  const immutable = filePath.includes('/assets/')

  res.writeHead(200, {
    'Content-Type': type,
    'Cache-Control': immutable ? 'public, max-age=31536000, immutable' : 'no-cache',
  })
  createReadStream(filePath).pipe(res)
}

const server = createServer((req, res) => {
  const pathname = (req.url ?? '/').split('?')[0]

  // The container's own liveness probe. It does not touch the backend, so a
  // backend outage does not restart the console — the console is still
  // serving (and showing its stale-data banner).
  if (pathname === '/healthz') {
    res.writeHead(200, { 'Content-Type': 'application/json; charset=utf-8' })
    res.end(JSON.stringify({ status: 'ok', service: 'buetcsefest-frontend' }))
    return
  }

  if (pathname.startsWith('/api/')) {
    proxy(req, res)
    return
  }

  serveStatic(req, res)
})

server.listen(PORT, '0.0.0.0', () => {
  console.log(`operator console on http://0.0.0.0:${PORT} -> ${BACKEND}`)
})
