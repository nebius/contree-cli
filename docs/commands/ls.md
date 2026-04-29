# ls

List files and directories in the session image without spawning a sandbox.

## Examples

```bash
# List root directory
contree ls /

# List a specific directory
contree ls /etc/nginx

# JSON output with file metadata
contree -f json ls /usr/bin
```

## Help output

```{terminal-shell} contree ls --help
```

## Output

Each entry shows path, size, permissions (octal), owner, group, modification
time, and type (`d` for directory, `l` for symlink, `-` for file).

This command reads the image filesystem directly -- no sandbox is started and
no resources are consumed.

## See also

- {doc}`cat` -- view file contents
- {doc}`cp` -- download a file locally
- {doc}`/tutorial/first-steps` -- inspecting the filesystem
