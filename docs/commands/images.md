# images

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

## See also

- {doc}`tag` -- assign a tag to an image
- {doc}`/tutorial/first-steps` -- browsing and choosing images
