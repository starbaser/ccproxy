# BUG: 502 Bad Gateway — Reverse proxy falls through to localhost:1 placeholder

## Symptom

Intermittent 502 Bad Gateway on reverse proxy flows. The client receives a connection refused error. The request never reaches the upstream provider.

## Root Cause (observed)

mitmproxy's reverse proxy listener uses `localhost:1` as a placeholder backend. The transform handler (`_resolve_transform_target` in `inspector/routes/transform.py`) is supposed to rewrite the destination per-flow before mitmproxy connects upstream. When the transform handler doesn't fire (or doesn't match), the flow attempts to connect to `localhost:1`, which refuses:

```
error establishing server connection: Multiple exceptions:
  [Errno 111] Connect call failed ('::1', 1, 0, 0),
  [Errno 111] Connect call failed ('127.0.0.1', 1)
```

The client sees HTTP 502.

## Evidence

Journal log from 2026-04-28:

```
14:54:28 xepor.xepor  INFO  => [200] /v1internal:generateContent   ← previous request succeeded
14:54:28 mitmproxy     INFO  server disconnect cloudcode-pa:443
14:54:28 mitmproxy     INFO  client disconnect
14:54:52 mitmproxy     INFO  client connect                         ← new request, 24s later
14:54:52 mitmproxy     INFO  error establishing server connection:  ← immediate failure
         Connect call failed ('::1', 1, 0, 0),
         Connect call failed ('127.0.0.1', 1)
14:54:52 ccproxy.inspector.addon  WARNING  Request error: ... (trace_id: 73c51631-...)
```

Key observations:
- No xepor route log for the failing request — the transform REQUEST handler never ran
- The request path was `/v1internal:generateContent` which SHOULD match the `/v1internal` redirect rule
- The immediately preceding request to the same path succeeded
- The failing request carried a ~4 MB base64-encoded video payload (3.1 MB raw mp4)

Flow dump confirms `localhost:1` destination:
```
POST http://localhost:1/v1internal:generateContent → status 0 (no response)
```

## Hypotheses

### H1: Large payload body parsing failure (medium confidence)

The transform handler parses the request body as JSON to extract `model` for `match_model` matching. A ~4 MB base64 payload means a ~5.3 MB JSON body. If body parsing fails or times out, `_resolve_transform_target` returns `None`, and reverse proxy flows with no match get the placeholder backend.

The code path:
```python
# routes/transform.py handle_transform()
body = json.loads(flow.request.content or b"{}")  # ~5 MB JSON parse
target = _resolve_transform_target(flow, body)
if target is None:
    # ReverseMode → respond 501... but maybe something else happens?
```

### H2: xepor route handler not invoked (medium confidence)

xepor's `InterceptedAPI` matches routes by path template. If the route matching fails silently (e.g., path normalization issue, or a previous router set a passthrough flag), the transform handler never runs. The request goes directly to mitmproxy's connection layer with the placeholder `localhost:1`.

The `InspectorRouter` overrides have passthrough guards:
```python
def request(self, flow):
    if not self._has_request_routes:
        return  # short-circuit, don't set passthrough flag
```

If the addon chain order breaks or a previous addon sets an unexpected state, downstream routers may be skipped.

### H3: mitmproxy race condition (low confidence)

mitmproxy may attempt the upstream connection before the addon chain's request hooks have a chance to rewrite the destination. Under high load or with large payloads, the connection to `localhost:1` could be initiated prematurely.

## Reproduction

Observed with:
```bash
uv run glass ~/dev/projects/polarviz/output/manifold/manifold-morph.mp4 \
  -p "Describe what happens in this video."
```

Glass sends to `http://127.0.0.1:4000/v1internal:generateContent` with:
- `x-api-key: sk-ant-oat-ccproxy-gemini` (sentinel key)
- ~5.3 MB JSON body (3.1 MB mp4 → base64 inline)

The same path (`/v1internal:generateContent`) with a 588 KB image succeeded immediately before.

## Impact

- Reverse proxy clients get 502 with no diagnostic info
- The error is non-deterministic — same path/config works on smaller payloads
- Reported as persistent across sessions (not just this occurrence)

## Additional Error Pattern

Unrelated but co-occurring: repeated `'list' object has no attribute 'get'` errors from `play.googleapis.com/log` traffic hitting the pipeline. These are Google telemetry requests with JSON array bodies (not objects) that the pipeline context parser doesn't handle. Not causing the 502 but polluting the logs.
