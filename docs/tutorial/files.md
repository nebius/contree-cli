# Working with Files

contree-cli lets you inject local files into sandboxes, edit remote files
in-place, and stage changes that automatically apply on the next run.

## Inject a file with `--file`

Use `--file` / `-F` on `contree run` to attach a local file:

::::{tab-set}
:::{tab-item} CLI
```bash
contree run --file ./app.py python /app.py
```
:::

:::{tab-item} Shell
```text
contree run --file ./app.py python /app.py
```

Flags like `--file` require the explicit `contree run` prefix.
:::
::::

By default the file is placed at the same path inside the sandbox.
Specify a different destination with a colon:

::::{tab-set}
:::{tab-item} CLI
```bash
contree run --file ./app.py:/app/main.py python /app/main.py
```
:::

:::{tab-item} Shell
```text
contree run --file ./app.py:/app/main.py python /app/main.py
```
:::
::::

### Full `--file` syntax

```
host_path[:instance_path][:uUID][:gGID][:mMODE]
```

- **host_path** -- path to the file on your machine (required)
- **instance_path** -- destination path inside the sandbox (detected by
  leading `/`). Defaults to the host path.
- **uUID** -- owner UID (prefix `u`). Numeric or name (resolved locally).
- **gGID** -- group GID (prefix `g`). Numeric or name (resolved locally).
- **mMODE** -- octal permission mode (prefix `m`).

Tagged options (`u`, `g`, `m`) can appear in any order after the host
path. Unspecified values default to the host file's stat.

```bash
# Override only mode
contree run --file ./script.sh:m0755 /script.sh

# All explicit
contree run --file ./app.py:/app.py:u0:g0:m0755 python /app.py

# Named uid/gid (resolved from local system)
contree run --file ./app.py:uroot:groot python /app.py
```

:::{note}
Named uid/gid (e.g. `uroot`) are resolved locally via `pwd`/`grp`
modules. Use numeric IDs if unsure about host/instance mismatch.
:::

### Multiple files

Repeat the `--file` flag:

```bash
contree run \
  --file ./app.py:/app.py \
  --file ./config.yaml:/etc/app/config.yaml \
  python /app.py
```

### Directories

`--file` also accepts directories. The entire tree is uploaded recursively:

```bash
contree run --file ./src:/app/src -- make -C /app/src
```

Common junk is excluded by default: `.*`, `.git`, `*.pyc`, `__pycache__`,
`.venv`, `.mypy_cache`, `.pytest_cache`, `node_modules`, `dist`, `build`.

Add extra exclusions with `--file-excludes`:

```bash
contree run --file ./project:/app --file-excludes '*.log' '*.tmp' -- make -C /app
```

### Upload caching

The CLI keeps a local upload cache keyed by file path, inode, modification
time, and size. Repeated attachments of unchanged files skip both the hash
calculation and the API call. The cache expires after 90 days to account
for server-side file retention.

The server also deduplicates by SHA256 — if the same content was uploaded
from a different session or machine, it is reused without re-uploading.

## Edit remote files

::::{tab-set}
:::{tab-item} CLI
`contree file edit` downloads a file from the session image, opens it
in your `$EDITOR`, and stages the changes as a pending file:

```bash
contree file edit /etc/nginx/nginx.conf
```
:::

:::{tab-item} Shell
In the interactive shell, `vim`, `vi`, and `nano` are aliases for
`contree file edit`, using your host `$EDITOR`:

```text
vim /etc/nginx/nginx.conf
```

You can also use the full command:

```text
contree file edit /etc/nginx/nginx.conf
```
:::
::::

What happens step by step:

1. The file is downloaded from the current session image to a temp file
2. Your editor opens (`$EDITOR`, defaults to `vi`)
3. If you saved changes, the modified file is uploaded and staged as pending
4. If the file is unchanged (same SHA256), nothing is staged

If the file does not exist in the image yet, an empty file is created for
you to fill in.

### Iterate without re-running

You can edit multiple files before running:

::::{tab-set}
:::{tab-item} CLI
```bash
contree file edit /etc/nginx/nginx.conf
contree file edit /etc/nginx/sites-enabled/default
contree run nginx -t                # test config with both edits applied
```
:::

:::{tab-item} Shell
```text
vim /etc/nginx/nginx.conf
vim /etc/nginx/sites-enabled/default
nginx -t
```
:::
::::

Both edits are staged as pending and applied together on the next run.

## Stage local files with `contree file cp`

`contree file cp` copies a local file into the session as a pending file:

::::{tab-set}
:::{tab-item} CLI
```bash
contree file cp ./config.yaml /etc/app/config.yaml
```
:::

:::{tab-item} Shell
```text
contree file cp ./config.yaml /etc/app/config.yaml
```
:::
::::

This uploads the file and records it as pending -- it does not run anything.
The file will be included in the next `contree run` automatically.

### Build up a working environment

::::{tab-set}
:::{tab-item} CLI
```bash
contree file cp ./app.py /app/app.py
contree file cp ./requirements.txt /app/requirements.txt
contree file cp ./config.yaml /etc/app/config.yaml
contree run pip install -r /app/requirements.txt
contree run python /app/app.py
```
:::

:::{tab-item} Shell
```text
contree file cp ./app.py /app/app.py
contree file cp ./requirements.txt /app/requirements.txt
contree file cp ./config.yaml /etc/app/config.yaml
pip install -r /app/requirements.txt
python /app/app.py
```
:::
::::

The first `run` consumes all three pending files and bakes them into the
new image. The second `run` already has them -- no re-upload needed.

## How pending files work

Pending files accumulate until the next `contree run`:

1. Each `file edit` or `file cp` records a pending file in the session
2. When you run `contree run`, all pending files are merged into the payload
3. After the run completes, the new image already contains those files
4. The pending queue is effectively cleared (a new history checkpoint is
   created past them)

Explicit `--file` flags on `contree run` take priority over pending files
at the same path.

Pending files are branch-aware -- switching branches with
`contree session checkout` changes which pending files are visible.

## Deduplication

Files are uploaded to the API with SHA256 dedup. If the same file content
has already been uploaded (from a previous edit or a different session), it
is reused without re-uploading.

---

You can inject and edit files. Next: {doc}`images`.
