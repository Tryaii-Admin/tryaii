# Routing daemon

`tryaii route` (and `tryaii eval`) pay a large fixed startup cost on every
invocation: importing the embedding stack (torch / `@xenova/transformers`) and
loading the embedding model dominates wall-clock time, while the routing math
itself takes well under a millisecond. Because each CLI call is a fresh process,
that cost cannot be amortized within a single invocation -- caching centroids
only avoids *regenerating* them; the model still loads from scratch.

The daemon fixes this. A long-lived background process loads the model once and
keeps it warm; subsequent `route`/`eval` calls become thin clients that send the
prompt over a loopback socket and get the routing decision back near-instantly.

Both SDKs (Python and Node) implement the same design and the same CLI surface.
They run **separate** daemons: the Python SDK embeds with `sentence-transformers`
and the Node SDK with `@xenova/transformers`, so their embeddings (and therefore
routing) are not guaranteed bit-identical. A client only ever talks to a daemon
started by the same runtime and for the same embedding model (verified on the
ping handshake); otherwise it starts its own.

## CLI surface

```
tryaii route "..."            # auto-starts a daemon on first call, then reuses it
tryaii route "..." --no-daemon  # force in-process routing for this call
tryaii serve                  # run the daemon in the foreground (Ctrl-C to stop)
tryaii daemon start|stop|status|restart
```

Environment variables:

- `TRYAII_NO_DAEMON=1` -- never use or start a daemon; always route in-process.
- `TRYAII_DAEMON_IDLE=<seconds>` -- shut the daemon down after this many idle
  seconds (default 900). `0` disables the idle timeout.
- `TRYAII_DAEMON_WAIT=<seconds>` -- how long an auto-start waits for the daemon
  to finish loading before giving up and routing in-process (default 180).

## State file

Each runtime writes a state file in the data dir (`~/.tryaii/` by default):

- Python: `~/.tryaii/daemon-python.json`
- Node:   `~/.tryaii/daemon-node.json`

The file is written `0600` and only *after* the model is warm, so its presence
signals readiness. Shape:

```json
{
  "runtime": "python",
  "version": "0.3.0",
  "embeddingModel": "all-MiniLM-L6-v2",
  "host": "127.0.0.1",
  "port": 0,
  "token": "<random hex>",
  "pid": 0,
  "startedAtMs": 0
}
```

## Wire protocol

Loopback TCP (`127.0.0.1`, OS-assigned port), one request and one response per
connection, each a single UTF-8 JSON object terminated by `\n`. Every request
must carry the `token` from the state file; the server rejects mismatches with
`{"ok": false, "error": "unauthorized"}`. Binding to loopback plus the token
keeps other local users off the socket.

Request:

```json
{"v": 1, "token": "...", "cmd": "route", "prompt": "...",
 "priorities": {"quality": 3, "cost": 3, "speed": 3}, "topK": 5}
```

`cmd` is one of `route`, `ping`, `shutdown`.

Response (route):

```json
{"ok": true, "result": {
  "bestModel": "...",
  "scores": [{
    "modelId": "...", "finalScore": 0.0, "qualityScore": 0.0,
    "costScore": 0.0, "speedScore": 0.0, "qualityContribution": 0.0,
    "costContribution": 0.0, "speedContribution": 0.0,
    "topBenchmarks": [["name", 0.0]], "reasoning": "..."
  }],
  "classification": {
    "benchmarkScores": {"name": 0.0}, "broadCategory": "...",
    "subcategory": "...", "confidence": 0.0, "classifierUsed": "embedding",
    "cacheHit": false, "processingTimeMs": 0.0, "difficulty": 0.0
  },
  "priorities": {"quality": 3, "cost": 3, "speed": 3}
}}
```

Response (ping): `{"ok": true, "pong": true, "runtime": "...", "version": "...",
"embeddingModel": "...", "pid": 0, "uptimeMs": 0}`.

Response (shutdown): `{"ok": true, "bye": true}`.

Errors: `{"ok": false, "error": "<message>"}`.
```
