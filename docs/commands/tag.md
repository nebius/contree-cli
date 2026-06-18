---
icon: tag
---

# tag - Tag or untag an image

Assign or remove a tag from an image. Tags are human-readable names that
make images easier to reference.

## Examples

```bash
# Tag an image
contree tag 3f2a7b... my-app:v1.0

# Remove a tag
contree tag 3f2a7b... my-app:v1.0 --delete

# Use the tagged image
eval $(contree use tag:my-app:v1.0)
```

## Help output

```{terminal-shell} contree tag --help
```

## Usage

Tags are free-form strings. A common convention is `scope/purpose:version`:

```bash
contree tag UUID ubuntu-with-curl:latest
contree tag UUID my-project/dev-env:v2
```

Once tagged, reference the image anywhere with the `tag:` prefix:

```bash
contree use tag:ubuntu-with-curl:latest
```

Tagging an image that already has a different tag replaces the old tag.

## See also

- {doc}`images` -- list images and their tags
- {doc}`/tutorial/first-steps` -- working with images
