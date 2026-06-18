---
icon: file-lines
---

# cat - Show file content from the image

Display the contents of a file from the session image.

## Examples

```bash
# View a text file
contree cat /etc/os-release

# Pipe to another command
contree cat /var/log/app.log | grep ERROR

# Redirect to a local file
contree cat /etc/nginx/nginx.conf > nginx.conf
```

## Help output

```{terminal-shell} contree cat --help
```

## Behavior

The file is read directly from the image -- no sandbox is started.

Binary files are detected and refused when output is a terminal (to protect
your shell). Redirect to a file or pipe to another command to handle binary
content:

```bash
contree cat /usr/bin/curl > curl
```

For downloading files to a specific local path, use {doc}`cp` instead.

## See also

- {doc}`ls` -- list files before viewing
- {doc}`cp` -- download to a local path with progress
