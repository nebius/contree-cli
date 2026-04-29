# shell

Start an interactive REPL for managing sessions and running sandbox commands.

## Help output

```{terminal-shell} contree shell --help
```

## Examples

```bash
# Start the shell
contree shell

# Start with a specific output format
contree -f json shell

# Start with a named profile
contree --profile=personal shell
```

## Prompt

The prompt shows the current working directory:

```text
contree:/> apt-get update -qq
contree:/app> python main.py
```

## Command dispatch

The shell recognises four types of input:

**Bare commands** — executed inside the sandbox as an implicit `contree run`
with `shell=True`:

```text
apt-get install -y curl
echo hello && ls /
```

**Prefixed commands** — `contree ...` dispatches management commands through
the same argparse parser as the CLI:

```text
contree ls /etc
contree session branch experiment
contree run -e DEBUG=1 -- ./app
```

**Builtins** — handled locally by the shell:

| Builtin | Description |
|---------|-------------|
| `cd [PATH]` | Change working directory (`cd -` for previous) |
| `pwd` | Print working directory |
| `history [N]` | Show command history (optional limit) |
| `help [TOPIC]` | Show help (optionally for a specific command) |
| `clear` | Clear the terminal screen |
| `--format NAME` / `-f NAME` | Change output format (or show current if no argument) |
| `exit` / `quit` | Exit the shell (also Ctrl-D) |

**Aliases** — bare names intercepted for convenience:

| Alias | Equivalent |
|-------|-----------|
| `ls [PATH]` | `contree ls [PATH]` (API inspect, no sandbox) |
| `cat PATH` | `contree cat PATH` (API inspect, no sandbox) |
| `vim PATH` | `contree file edit PATH` (with `EDITOR=vim`) |
| `vi PATH` | `contree file edit PATH` (with `EDITOR=vi`) |
| `nvim PATH` | `contree file edit PATH` (with `EDITOR=nvim`) |
| `nano PATH` | `contree file edit PATH` (with `EDITOR=nano`) |

:::{note}
`ls` and `cat` aliases fall back to running inside the sandbox when pending
files exist or when args contain flags or glob characters.
:::

## Tab completion

The shell provides context-aware tab completion for almost everything
except bare (implicit run) commands. Press Tab to complete:

| Context | What completes |
|---------|---------------|
| Empty prompt | All commands, aliases, and builtins |
| `contree <TAB>` | Subcommand names |
| `contree CMD -<TAB>` | Flags for that command |
| `contree CMD --<TAB>` | Long flags for that command |
| `ls /etc/<TAB>`, `cat /etc/<TAB>` | Sandbox file paths |
| `cd /us<TAB>` | Sandbox directory paths (dirs only) |
| `vim /etc/<TAB>`, `nano /etc/<TAB>` | Sandbox file paths |
| `contree use <TAB>` | Image UUIDs and `tag:NAME` |
| `contree tag <TAB>` | Image UUIDs and `tag:NAME` |
| `contree show <TAB>` | Operation UUIDs |
| `contree kill <TAB>` | Operation UUIDs |
| `contree session checkout <TAB>` | Branch names |
| `contree session branch <TAB>` | Branch names |
| `contree session use <TAB>` | Session keys |
| `contree file edit <TAB>` | Sandbox file paths |
| `help <TAB>` | All command and alias names |

Path completions query the sandbox filesystem via the inspect API
and are cached persistently -- subsequent completions for the same
directory are instant.

## Line continuation

A trailing `\` at the end of input triggers a `> ` continuation prompt,
just like traditional shells:

```text
contree:/> ls \
> -alh \
> /sys
```

Backslash-newline pairs are removed to join the lines into a single
command (`ls -alh /sys`). Unclosed quotes also trigger continuation,
preserving the newline inside the quoted string.

## Limitations

- **No global flags on commands**: `--token`, `--url`, `--log-level` are
  not available inside the shell.
- **No local pipes or redirects**: `|`, `>`, `<` are passed as-is to the
  sandbox (works for remote commands, not for contree output).
- **No job control**: No `&`, `bg`, `fg`, or Ctrl-Z. Use `contree run -d`
  for background tasks.
- **Bare commands use defaults**: No way to pass `--timeout`, `--env`, or
  `--file` without the explicit `contree run` prefix.
- **No `~` or glob expansion**: Passed as-is to the sandbox.
- **Cannot nest shells**: Running `contree shell` inside a shell is not
  supported.

## See also

- {doc}`/tutorial/shell` -- full tutorial on using the interactive shell
- {doc}`run` -- the `run` command used by implicit bare commands
- {doc}`file` -- the `file edit` command behind editor aliases
