# skill

Install, remove, or upgrade ConTree agent skills for Codex and Claude Code.

## Spec format

Commands accept **specs** — a `kind:hint` URI that identifies the skill type
and target path:

| Spec | Resolves to | Skill type |
|------|------------|------------|
| `claude:` | `.claude/skills/contree` (project-level) | ClaudeSkill |
| `claude:~` | `~/.claude/skills/contree` (global) | ClaudeSkill |
| `codex:` | `.codex/skills/contree` (project-level) | OpenAISkill |
| `codex:~` | `~/.codex/skills/contree` (global) | OpenAISkill |
| `claude-agent:` | `.claude/agents/contree.md` (project) | ClaudeAgentSkill |
| `claude-agent:~` | `~/.claude/agents/contree.md` (global) | ClaudeAgentSkill |
| `claude://./path` | `./path` (explicit) | ClaudeSkill |
| `./path` | `./path` (guessed from path) | auto |

When no specs are given, `install` defaults to all detected agent homes
(e.g. `codex:~ claude:~ claude-agent:~`).

## Examples

```bash
# Install globally (default: codex:~ claude:~)
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

## Behavior

`contree skill install [SPEC ...]` installs skill directories. Each spec
resolves to a skill class and a filesystem path. The skill class (`kind`)
is stored in the session SQLite database alongside the resolved path.

`contree skill list` shows remembered installs with their kind,
installed version, latest available version, outdated flag, and
whether the path exists on disk.

`contree skill upgrade [SPEC ...]` overwrites existing installs with the
current bundled version. Without specs, upgrades all remembered locations.
A `.version` file in each install directory tracks the installed version
for quick outdated detection.

`contree skill remove SPEC [...]` deletes installed skill files and
forgets the path from the registry.

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
