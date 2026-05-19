# show - Inspect an operation

:::{note}
**`contree show` is a top-level shortcut for {doc}`operation show <operation>` (`contree op show`).**

Both share one argparse setup and one handler. The top-level `show`
accepts one or more UUIDs and `@N` session-history references — each
UUID renders as its own row. See the {doc}`operation` page for the
full description.
:::

Display the full result of one or more operations, including stdout
and stderr from sandbox execution.

## Examples

```bash
# Show a single operation
contree show 3f2a7b...

# Show multiple operations in one call
contree show 3f2a7b... a1b2c3... 9d8e7f...

# History references (resolved against the active session)
contree show @5 @4 @3

# JSON output for scripting
contree -f json show 3f2a7b...

# Show result of a detached run
contree run -d -- make test
contree show UUID
```

## Help output

```{terminal-shell} contree show --help
```

## Output

The command renders every scalar top-level field the API returns
(typically: **uuid**, **kind**, **status**, **created_at**,
**started_at**, **finished_at**, **duration**, **session_key**, …) and
adds these derived fields:

- **exit_code** -- the sandbox process exit code (extracted from
  `metadata.result.state.exit_code`)
- **image** -- resulting image UUID from `result.image`
- **tag** -- image tag from `result.tag`
- **status** -- promoted to `FAILED` if the API reported `SUCCESS` but
  the process exited with a non-zero exit code
- **stdout / stderr** -- sandbox output, decoded (for `default`,
  `json`, and `json-pretty` formats)

`error` is pinned to the last column. Nested objects (`metadata`,
`result`) are dropped from the flat row — use `-f json` if you need
them verbatim, or `op show` for the inspector view.

Timestamps come back from the API in UTC and are converted to the
**local timezone** for human-readable formatters (`default`, `table`,
`csv`, `tsv`, `plain`). The JSON formatters preserve the source
timezone offset.

For `csv`, `tsv`, and `table` formats, stdout/stderr are omitted -- use
`default` or `json` to see sandbox output.

## See also

- {doc}`ps` -- list operations to find UUIDs
- {doc}`operation` -- multi-UUID variant: `contree op show UUID1 UUID2 ...`
- {doc}`run` -- the command that creates operations
