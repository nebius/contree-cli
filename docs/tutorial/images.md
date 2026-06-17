---
icon: layer-group
---

# Images & Tags

Images are filesystem snapshots. Every non-disposable `contree run`
produces a new image. Tags give images human-readable names so you
can find and reuse them later.

All data — images, operations, and uploaded files — is scoped to a
**Project**. Multiple tokens can access the same project and share
its data. Different projects have separate scopes — nothing is
visible across project boundaries.

## Listing images

Show tagged images:

```bash
contree images
```

Filter by tag prefix:

```bash
contree images --prefix=ubuntu
contree images --prefix=my-app
```

Include untagged images:

```bash
contree images -a
```

Filter by time:

```bash
contree images --since 1d        # last 24 hours
contree images --since 2025-04-01
```

## Tagging images

With one argument, tags the current session image. With two, the first
is the image reference:

```bash
contree tag my-app:v1.0                      # current session image
contree tag UUID my-app:v1.0                 # specific image by UUID
contree tag tag:alpine:latest my-alpine      # re-tag by reference
```

The CLI resolves tag references to UUIDs automatically — you don't
need to look up the UUID first.

Tags follow a free-form `name:version` convention. Common patterns:

```bash
contree tag UUID my-app:latest
contree tag UUID my-app:v2.0
contree tag UUID common/python-ml/python:3.11-slim
```

Remove a tag:

```bash
contree tag -d UUID my-app:v1.0
```

### Tag rules

- Tags are scoped to your Project (API token)
- Each image can have multiple tags
- Tags are unique — assigning an existing tag to a different image **moves** it
- Allowed characters: `a-z`, `0-9`, `_`, `-`, with `:`, `/`, `.` as separators
- Max length: 256 characters
- Case-sensitive (lowercase recommended)

### Shadow behavior

Public images (like `ubuntu:latest`) have their own tags. When you assign
the same tag to your own image, the public image is still accessible by
UUID but its tag becomes shadowed. Removing your tag restores the public
one.

## Using tags

Use `tag:NAME` anywhere an image UUID is expected:

```bash
contree use tag:my-app:latest
```

If both you and a public image share a tag, your image wins.

## Importing images

Pull images from container registries (Docker Hub, GHCR, etc.):

```bash
contree images import ubuntu:latest
contree images import --timeout 600 ubuntu:latest
contree images import python:3.11-slim
contree images import ghcr.io/org/repo:tag
```

Import is asynchronous — the CLI polls until the operation completes.
Press Ctrl+C to cancel.

Import multiple at once:

```bash
contree images import ubuntu:latest python:3.11-slim node:20-slim
```

### Private registries

Authenticate with `--username`:

```bash
contree images import --username=user registry.example.com/image:tag
```

The password is prompted securely if `--username` is provided.
Credentials are used only for the import operation and discarded
immediately after — the server does not store them.

## Reusing images across sessions

A common workflow is to prepare a base environment, tag it, and
reuse it in future sessions:

```bash
# Session 1: build the environment
contree use tag:ubuntu:latest
contree run apt-get update -qq
contree run apt-get install -y python3 python3-pip build-essential
contree tag UUID python-dev:latest

# Session 2 (days later): start from the prepared image
contree use tag:python-dev:latest
contree run pip install -r requirements.txt
```

:::{tip}
Search for existing tagged images before rebuilding:

```bash
contree images --prefix=python-dev
```
:::

---

Images are your checkpoints. Next: {doc}`build`.
