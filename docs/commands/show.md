# show

Display the full result of an operation, including stdout and stderr from
sandbox execution.

## Examples

```bash
# Show operation details
contree show 3f2a7b...

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

The command displays:

- **uuid**, **kind**, **status**, **duration** -- operation metadata
- **exit_code** -- the sandbox process exit code (if completed)
- **error** -- error message (if failed)
- **image** -- resulting image UUID (for non-disposable runs)
- **tag** -- image tag (if assigned)
- **stdout / stderr** -- sandbox output (for `default`, `json`, and
  `json-pretty` formats)

For `csv`, `tsv`, and `table` formats, stdout/stderr are omitted -- use
`default` or `json` to see sandbox output.

## See also

- {doc}`ps` -- list operations to find UUIDs
- {doc}`operation` -- multi-UUID variant: `contree op show UUID1 UUID2 ...`
- {doc}`run` -- the command that creates operations
