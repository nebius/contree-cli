---
icon: layer-group
---

# images - List and import images

List images in the project. Images are the filesystem snapshots that sandboxes
run from -- every non-disposable `contree run` produces a new one.

## Examples

```bash
# List all images
contree images

# Filter by tag prefix
contree images --prefix=ubuntu

# Only tagged images
contree images --tagged

# Images created in the last hour
contree images --since=1h

# Find a specific image by UUID prefix
contree images --uuid=3f2a7b

# JSON output for scripting
contree -f json images --tagged | jq -r '.tag'
```

## Help output

```{terminal-shell} contree images --help
```

## Filtering

`--prefix` matches the beginning of the tag string. This is useful for
browsing available base images:

```bash
contree images --prefix=python
contree images --prefix=common/
```

`--since` and `--until` accept either ISO timestamps or duration intervals
like `1h`, `30m`, `7d`.

## Subcommands

### `images list`

`contree images list` (alias `ls`) is the explicit form of the bare
`contree images` invocation. Both share the same flag set -- pick the
explicit form when you want a command that reads symmetrically with
`images import`, or in scripts that already use the subcommand style
everywhere.

```{terminal-shell} contree images list --help
```

### `images import`

`contree images import REF [REF ...]` pulls one or more images from an
external OCI registry into the project and waits for the import
operation to finish. Each reference may be a `docker://` URL or any
form the platform accepts; multiple refs are imported sequentially with
shared credentials, and Ctrl-C cancels the in-flight operation cleanly.

```{terminal-shell} contree images import --help
```

## See also

- {doc}`tag` -- assign a tag to an image
- {doc}`/tutorial/first-steps` -- browsing and choosing images
