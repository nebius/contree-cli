---
icon: wand-magic-sparkles
---

# skill - Install agent skills

Install, remove, or upgrade ConTree agent skills for Codex and Claude Code.

## Spec format

Commands accept **specs** -- a `kind:hint` URI that identifies the skill type
and target path:

| Spec | Resolves to | Skill type |
|------|------------|------------|
| `claude:` | `.claude/skills/contree` (project) | ClaudeSkill |
| `claude:~` | `~/.claude/skills/contree` (global) | ClaudeSkill |
| `codex:` | `.codex/skills/contree` (project) | CodexSkill |
| `codex:~` | `~/.codex/skills/contree` (global) | CodexSkill |
| `opencode:` | `.opencode/skills/contree` (project) | OpenCodeSkill |
| `opencode:~` | `$OPENCODE_HOME/skills/contree` (or `~/.config/opencode/skills/contree`) | OpenCodeSkill |
| `amp:` | `.amp/skills/contree` (project) | AmpSkill |
| `amp:~` | `~/.config/agents/skills/contree` (global) | AmpSkill |
| `cline:` | `.cline/skills/contree` (project) | ClineSkill |
| `cline:~` | `$CLINE_DIR/skills/contree` (or `~/.cline/skills/contree`) | ClineSkill |
| `claude-subagent:` | `.claude/agents/contree-subagent.md` (project) | ClaudeSubagentSkill |
| `claude-subagent:~` | `~/.claude/agents/contree-subagent.md` (global) | ClaudeSubagentSkill |
| `claude-agent:` | `.claude/agents/contree.md` (project) | ClaudeAgentSkill |
| `claude-agent:~` | `~/.claude/agents/contree.md` (global) | ClaudeAgentSkill |
| `./path` | `./path` (skill type guessed from path) | auto |

When no specs are given, `install` targets the global (`:~`) variant of
**every** known kind. The Claude-based kinds (`claude`, `claude-agent`,
`claude-subagent`) are skipped unless `~/.claude` already exists, so a
machine without Claude Code installed will not have empty directories
created for it.

## Examples

```bash
# Install globally to every known kind (Claude kinds gated on ~/.claude)
contree skill install

# Install into project-level .claude/skills/contree
contree skill install claude:

# Install globally into ~/.claude/skills/contree
contree skill install claude:~

# Install both globally
contree skill install codex:~ claude:~

# Install to explicit path (class guessed from path)
contree skill install ./my/custom/path

# Upgrade all remembered installs
contree skill upgrade

# Upgrade specific target
contree skill upgrade claude:~

# Remove by spec
contree skill remove -y claude:~

# Remove by full path
contree skill remove -y /path/to/skills/contree

# List installs with version and outdated status
contree skill list
contree skill ls
```

## Help output

```{terminal-shell} contree skill --help
```

## Subcommands

### `skill install`

`contree skill install [SPEC ...]` (alias `i`) installs skill directories.
Each spec resolves to a skill class and a filesystem path, both of which
are persisted in `skills.db` so future `list` and `upgrade` calls can
find the install without re-specifying it. With no specs the command
targets the global (`:~`) variant of every known kind -- Claude-based
kinds are skipped automatically when `~/.claude` does not exist. Pass
`-y` to overwrite an existing install non-interactively.

```{terminal-shell} contree skill install --help
```

### `skill list`

`contree skill list` (alias `ls`) shows every remembered install with
its kind, installed version (read from `.version`), the version
bundled with this CLI, an `outdated` flag, and whether the install
path still exists on disk. Stale entries whose path was deleted
externally are pruned from the registry automatically when this
command runs.

```{terminal-shell} contree skill list --help
```

### `skill upgrade`

`contree skill upgrade [SPEC ...]` overwrites existing installs with
the version bundled in the current CLI. With no specs it upgrades
every remembered location, which is the normal post-`pip install -U`
maintenance step. Targets that are already at the bundled version are
rewritten anyway so any local edits to skill files are reverted.

```{terminal-shell} contree skill upgrade --help
```

### `skill remove`

`contree skill remove SPEC [...]` (aliases `r`, `rm`, `del`) deletes
installed skill files and forgets the path from the registry. Specs
may be the same URI form accepted by `install`, or a literal filesystem
path. Pass `-y` to skip the confirmation prompt.

```{terminal-shell} contree skill remove --help
```

### Install contents

Skill directories contain:

- `.version` — installed package version
- `SKILL.md` — skill prompt with `allowed-tools` frontmatter
- `agents/openai.yaml` — OpenAI-compatible skill config

Skills require `contree` in PATH. If missing, ask the user to install it.

### Skill classes

| Class | Kind | Description |
|-------|------|-------------|
| `ClaudeSkill` | `claude` | Bundled skill directory for Claude Code |
| `CodexSkill` | `codex` | Bundled skill directory for Codex |
| `OpenCodeSkill` | `opencode` | Bundled skill directory for OpenCode |
| `AmpSkill` | `amp` | Bundled skill directory for Amp |
| `ClineSkill` | `cline` | Bundled skill directory for Cline |
| `ClaudeSubagentSkill` | `claude-subagent` | Standalone `.md` subagent file |
| `ClaudeAgentSkill` | `claude-agent` | Custom agent `.md` with `skills: [contree]` |
