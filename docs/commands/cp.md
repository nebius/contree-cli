---
icon: copy
---

# cp - Download a file from the image

Download a file from the session image to a local path.

## Examples

```bash
# Copy a config file locally
contree cp /etc/nginx/nginx.conf ./nginx.conf

# Download a build artifact
contree cp /app/dist/output.tar.gz ./output.tar.gz
```

## Help output

```{terminal-shell} contree cp --help
```

## Behavior

The file is streamed from the image directly -- no sandbox is started.

For large files, progress is logged every 5 seconds with download speed
and ETA. The final log line shows total size and average speed.

## See also

- {doc}`ls` -- list files to find the path
- {doc}`cat` -- view file contents without downloading
- {doc}`/tutorial/first-steps` -- downloading files
