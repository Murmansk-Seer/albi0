# Per-run remote manifest snapshots

`await albi0.update_resources(..., on_remote_manifest=callback)` preserves the
existing `None` return value. The optional synchronous callback receives the
updater name and a `RemoteManifestSnapshot` with `version` and read-only `items`.
Item values are immutable `ManifestItem` objects (resource URL, local basename,
hash bytes and optional size). The snapshot is a detached copy before glob,
size and local-delta filtering. It is provided even if that filtering yields
no downloads, but only if this invocation actually reads a remote manifest.
An up-to-date version skip does not invent a snapshot or fetch another one.

Yoo manifests must match the requested package and version, have consistent
collection counts and unique bundle names. Callback errors abort the update
before resource download/publication and are never treated as network retries.
The callback is not global or persisted. A saved local incremental manifest
must not be substituted for a complete current-run snapshot.

Yoo metadata requests have one retry owner, at most three total attempts,
15-second connection and 30-second I/O timeouts, and a 60-second total deadline
per attempt. Logs omit credentials, request headers and query parameters.
