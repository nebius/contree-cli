# ConTree

{intro}

Use `contree` from PATH. If it is missing, ask the user to install it: `uv tool install contree-cli`, `pipx install contree-cli`, or `pip install contree-cli`.

## Codex Sandbox

`contree` needs network access and write access to its data directory: `$CONTREE_HOME`, or `$XDG_CONFIG_HOME/contree`, or `~/.config/contree`.

For default Codex config:

```toml
[sandbox_workspace_write]
network_access = true
writable_roots = ["~/.config/contree"]
```

If the user overrides `CONTREE_HOME` or `XDG_CONFIG_HOME`, the writable root must point at the resolved ConTree data directory. Without this, the CLI can fail with `sqlite3.OperationalError`. If the sandbox cannot be configured, stop and ask the user.

## Required Workflow

{first_step}
4. If syntax or behavior is unclear, consult the built-in manual before retrying: `contree agent <topic>` or `contree <command> --help`. Useful topics: `sessions`, `images`, `files`, `execution`, `output`, `profiles`, `command_safety`, `all_commands`, `all`.
5. Do not run bare or mutating auth commands. Agents may run read-only `contree -f json auth ls` / `auth profiles`; if auth is missing or invalid, ask the user to run `contree auth`.
6. Choose one explicit session key, then pass `-S <key>` on every current-session command: `use`, `run`, `cd`, `env`, `ls`, `cat`, `cp`, `file`, implicit-current-image `tag`, and current-session `session show/branch/checkout/rollback/wait`.
7. Before `use`, list available images with a prefix. Do not assume a tag exists: `contree images --prefix python`, `contree images --prefix ubuntu`, `contree images --prefix compiler/`. An empty result just means that prefix has no tags in this project — broaden or vary the prefix (`python` vs `python-`, `compiler/` vs `compiler/python/`) before importing or rebuilding.
8. Bootstrap: `contree -S <key> use <tag-or-image-from-list>` then `contree -S <key> cd /root`.
9. Inspect first with `ls`, `cat`, `session show`, `ps`/`op ls`, or `op show`. Mutate in small rollbackable steps.
10. After installing tools or setting up an environment, tag the result: `contree -S <key> tag <purpose/base:tag>`.

Project-scoped or explicit-target commands usually do not need `-S`: `images`, `auth ls/profiles`, `op ls/show/wait/cancel`, `skill`, `agent`, `build`, `session list`, `session show NAME`, and help.

## Running Commands

- `contree run` executes remotely. Host files are invisible unless attached with `--file` or staged with `contree file cp`.
- Every `run` starts a fresh microVM. You cannot exec into a previous run or connect to a server started in a previous run. Start the server and client in the same `run -s` command when needed.
- Prefer direct mode for plain executables: `contree -S <key> run -- make test`.
- Use shell mode only for shell features such as pipes, redirects, `&&`, `;`, or variable expansion: `contree -S <key> run -s -- 'echo $HOME && ls /'`. Do not wrap `-s` commands in your own `sh -c`.
- Use `run -- sh -lc '...'` only when a login shell is explicitly required. Prefer `env`, `-e`, `cd`, and `-C`.
- Keep one mutating step per non-disposable run. Avoid chaining setup, build, and test into one history entry.
- Use `contree -S <key> cd /path` or `run -C /path`; do not put `cd` inside shell expressions just to set the workdir.
- Use `--disposable` only for throwaway checks. Non-disposable runs persist the resulting image in session history.

## Files

- One-off attachment: `contree -S <key> run --file ./src:/work/src -- make -C /work/src`.
- Stage for future runs: `contree -S <key> file cp ./config.yaml /etc/app/config.yaml`.
- Edit a remote file and stage it: `contree -S <key> file edit /etc/app/config.ini`.

Prefer `--file` for files needed by one command. Use `file cp` only when the staged file should be injected into multiple future runs. Pending files are included in the next run, including disposable runs; they are cleared only after a successful non-disposable run commits them into the next image. Explicit `--file` mappings win over pending files at the same destination.

Directory attachments recurse and exclude common junk such as `.git`, hidden files, `__pycache__`, `.venv`, `node_modules`, `dist`, and `build`. Add patterns with `--file-excludes`.

## Sessions And Rollback

- Reuse sessions deliberately: `contree session list --filter <hint>`, then `contree -S <key> session show`.
- Branch before risky work: `contree -S <key> session branch experiment` then `contree -S <key> session checkout experiment`.
- `session rollback N` with positive `N` is an absolute history id, not "back N steps". Use: `session rollback` for one step back, `session rollback -- -3` for three steps back, `session rollback +1` for one step forward. Inspect with `session show` before rollback.

## Detached Work

- Start detached: `contree -S <key> -f json run -d --disposable -- pytest tests/a`.
- Capture UUIDs with global `-f json` before `run`; default detached output is not reliable for `jq`.
- `op wait UUID...` is a pure observer. It polls, prints one row per completion with the server-reported `status` (`SUCCESS`/`FAILED`/`CANCELLED`) and a separate `exit_code` column for the sandbox process. It does not advance session state. The CLI's own exit code is 1 when any op finished non-`SUCCESS`, or the sandbox `exit_code` when a `SUCCESS` op exited non-zero, so `op wait && next` still composes naturally.
- `op wait --all` is project-wide. Prefer explicit UUIDs when multiple agents or shells may share the project.
- `session wait` with no UUIDs drains detached operations spawned from this session's local cache. Successful non-disposable runs advance the active branch; disposable runs are recorded as disposable branches.
- `session wait UUID...` is only a polling form in current CLI behavior; it does not load pending metadata and does not advance the branch. For explicit UUID workflows, extract the result image from `op show` or wait output (`.image` / `result_image_uuid`) and then `contree -S <key> use "$IMG"` or tag it.

## Images, Imports, And Build

- Search before rebuilding: `contree images --prefix <prefix>`.
- Prefer importing ready-made registry images for common toolchains: `contree images import rust:1-slim`, `contree images import node:20-slim`, `contree images import golang:1.22-alpine`. Use `--timeout <seconds>` for long imports.
- Private registries use `--username`; password is prompted.
- If the repo has a Dockerfile, prefer `contree build` over replaying each step by hand. `build` owns its own `build:<hash>` session; `-S` is harmless but does not bind it to your agent session. Verify from a normal session after the build.
- Toolchain images often install binaries outside the default `PATH` (e.g. `golang:1.22-alpine` puts `go` at `/usr/local/go/bin/go`). After `use tag:<toolchain>`, probe `PATH` with `contree -S <key> run -- printenv PATH` and either set it for the session (`contree -S <key> env PATH=/usr/local/go/bin:/usr/local/bin:/usr/bin:/bin`) or per-run (`run -e PATH=...`). Pair with `--preserve-env` if you want the change baked into the image.

## Output And Automation

- Global flags go before the subcommand: `contree -f json images --prefix python`.
- Prefer structured output in automation: `json`, `json-pretty`, `csv`, or `tsv`. `toml` is available only on Python 3.11+.
- `json` is line-delimited for streaming and multi-row commands.
- Default `run` output prints raw stdout/stderr, not a structured row.
- `cat` and `cp` are content-oriented; do not parse them as table/json listings.

## Operation References

Anywhere `--help` shows a positional named `UUID_OR_REF` (`op show`, `op cancel`, `op wait`, top-level `show`/`kill`, `session wait`) the CLI accepts both real operation UUIDs and current-session history refs. Refs require `-S <key>` and a history entry whose `operation_uuid` is set (`use` entries have none — error is "has no operation UUID"). Accepted forms:

| Form | Meaning |
|---|---|
| `@`, `:`, `HEAD` | active branch tip |
| `@N`, `:N`, bare `N` | absolute history id `N` |
| `@-N`, `:-N`, `HEAD~N` | `N` steps back from the tip |
| `HEAD~` | shorthand for `HEAD~1` |
| `@+N`, `:+N` | `N` steps forward (latest child) |

When unsure, use `session show` to find the absolute id and pass that.

`contree op show --raw UUID_OR_REF...` (also `contree show --raw ...`) prints each operation's full server payload as JSONL — one compact JSON object per line, no derived columns, no stdout/stderr decoding. Use it when the flat row hides what you need (`metadata`, `resources`, raw `result.state`, …) or pipe it into `jq -c`.

## Subagents

This skill does not grant permission to spawn subagents. If the host allows subagents and the user/policy authorizes them, give every subagent a unique `-S` key and restate the critical ConTree rules in the subagent prompt. Never share a session across parallel subagents.

## Fallback

Use the built-in manual instead of carrying all reference material in this skill:

```bash
contree agent
contree agent sessions
contree agent images
contree agent files
contree agent execution
contree agent output
contree agent profiles
contree <command> --help
```

{fallback}{references}
