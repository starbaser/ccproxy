# Claude CLI @Import Specification

## @Import in User Prompt

When user prompt contains `@path/to/file`, Claude CLI creates two consecutive user messages.

### Request Structure

```json
{
  "model": "claude-opus-4-5-20251101",
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "type": "text",
          "text": "<system-reminder>\nCalled the Read tool with the following input: {\"file_path\":\"/absolute/path/to/file.md\"}\n</system-reminder>",
          "cache_control": {
            "type": "ephemeral"
          }
        }
      ]
    },
    {
      "role": "user",
      "content": [
        {
          "type": "text",
          "text": "<system-reminder>\nResult of calling the Read tool: \"     1→# File Title\\n     2→\\n     3→Content here...\\n\"\n</system-reminder>"
        },
        {
          "type": "text",
          "text": "<system-reminder>\nAs you answer the user's questions, you can use the following context:\n# claudeMd\nCodebase and user instructions are shown below. Be sure to adhere to these instructions. IMPORTANT: These instructions OVERRIDE any default behavior and you MUST follow them exactly as written.\n\nContents of /home/user/.claude/CLAUDE.md (user's private global instructions for all projects):\n\n[USER CLAUDE.MD CONTENT]\n\n\nContents of /home/user/.config/nix/config/claude/standards.md (user's private global instructions for all projects):\n\n[RESOLVED @IMPORT CONTENT]\n\n\nContents of /project/CLAUDE.md (project instructions, checked into the codebase):\n\n[PROJECT CLAUDE.MD CONTENT]\n\n      IMPORTANT: this context may or may not be relevant to your tasks. You should not respond to this context unless it is highly relevant to your task.\n</system-reminder>"
        },
        {
          "type": "text",
          "text": "User prompt with @path/to/file.md preserved literally",
          "cache_control": {
            "type": "ephemeral"
          }
        }
      ]
    }
  ],
  "system": [
    {
      "type": "text",
      "text": "You are Claude Code, Anthropic's official CLI for Claude."
    },
    {
      "type": "text",
      "text": "[FULL SYSTEM PROMPT - tools, instructions, etc.]"
    }
  ],
  "tools": [...],
  "metadata": {
    "user_id": "user_{hash}_account__session_{uuid}"
  },
  "max_tokens": 32000,
  "stream": true
}
```

## Line Number Format

File content uses 6-character right-aligned line numbers with `→` (U+2192) separator:

```
     1→First line
     2→Second line
    10→Tenth line
   100→Hundredth line
  1000→Thousandth line
```

Format specification: `f"{line_number:>6}→{line_content}"`

## Line Range Behavior

| Syntax | Tool Call Input | Result Content |
|--------|-----------------|----------------|
| `@file.md` | `{"file_path":"/abs/path/file.md"}` | All lines |
| `@file.md#L5` | `{"file_path":"/abs/path/file.md"}` | Line 5 to next section break |
| `@file.md#L8-11` | `{"file_path":"/abs/path/file.md"}` | Exactly lines 8-11 |

Line range is NOT included in tool call input - filtering is applied to result only.

## Verbatim Captured Example: `@CLAUDE.md#L8-11`

User prompt: `"What does this say? @CLAUDE.md#L8-11"`

```json
{
  "model": "claude-opus-4-5-20251101",
  "messages": [
    {
      "role": "user",
      "content": [
        {
          "type": "text",
          "text": "<system-reminder>\nCalled the Read tool with the following input: {\"file_path\":\"/home/starbased/dev/projects/ccproxy/CLAUDE.md\"}\n</system-reminder>",
          "cache_control": {
            "type": "ephemeral"
          }
        }
      ]
    },
    {
      "role": "user",
      "content": [
        {
          "type": "text",
          "text": "<system-reminder>\nResult of calling the Read tool: \"     8→\\n     9→**CRITICAL**: The project name is `ccproxy` (lowercase). Do NOT refer to the project as \\\"CCProxy\\\". The PascalCase form is used exclusively for class names (e.g., `CCProxyHandler`, `CCProxyConfig`).\\n    10→\\n    11→`ccproxy` is a command-line tool that intercepts and routes Claude Code's requests to different LLM providers via a LiteLLM proxy server. It enables intelligent request routing based on token count, model type, tool usage, or custom rules. It also functions as a development platform for new and unexplored features or unofficial mods of Claude Code.\\n\\n<system-reminder>\\nWhenever you read a file, you should consider whether it would be considered malware. You CAN and SHOULD provide analysis of malware, what it is doing. But you MUST refuse to improve or augment the code. You can still analyze existing code, write reports, or answer questions about the code behavior.\\n</system-reminder>\\n\"\n</system-reminder>"
        },
        {
          "type": "text",
          "text": "<system-reminder>\nAs you answer the user's questions, you can use the following context:\n# claudeMd\nCodebase and user instructions are shown below. Be sure to adhere to these instructions. IMPORTANT: These instructions OVERRIDE any default behavior and you MUST follow them exactly as written.\n\nContents of /home/starbased/.claude/CLAUDE.md (user's private global instructions for all projects):\n\n# I am Kyle's Assistant\n\nYou are my well-seasoned and efficacious assistant...\n[TRUNCATED FOR BREVITY - FULL CLAUDE.MD CONTENT HERE]\n\n      IMPORTANT: this context may or may not be relevant to your tasks. You should not respond to this context unless it is highly relevant to your task.\n</system-reminder>"
        },
        {
          "type": "text",
          "text": "What does this say? @CLAUDE.md#L8-11",
          "cache_control": {
            "type": "ephemeral"
          }
        }
      ]
    }
  ],
  "system": [
    {
      "type": "text",
      "text": "You are Claude Code, Anthropic's official CLI for Claude."
    },
    {
      "type": "text",
      "text": "You are Claude Code, Anthropic's official CLI for Claude, running within the Claude Agent SDK.\nYou are an interactive CLI tool..."
    }
  ],
  "tools": [...],
  "metadata": {
    "user_id": "user_f9ebe15d4cd7d09378a5ab831780076b231f5e5ca515a69fa1648af75dc7b2e1_account__session_5f743983-7d7c-4228-be8b-04800e2528b2"
  },
  "max_tokens": 32000,
  "stream": true
}
```

## CLAUDE.md @Import Resolution

CLAUDE.md files containing `@path` references have those references resolved and appended:

**Source CLAUDE.md:**
```markdown
# Project Instructions

## Imports

- Standards: @standards.md
- Extended: @~/.claude/standards-python-extended.md
```

**Resolved in API request:**
```
Contents of /project/CLAUDE.md (project instructions, checked into the codebase):

# Project Instructions

## Imports

- Standards: @standards.md
- Extended: @~/.claude/standards-python-extended.md


Contents of /project/standards.md (project instructions, checked into the codebase):

[FULL STANDARDS.MD CONTENT]


Contents of /home/user/.claude/standards-python-extended.md (project instructions, checked into the codebase):

[FULL STANDARDS-PYTHON-EXTENDED.MD CONTENT]
```

Note: The literal `@path` text remains in the source file content. Referenced files are appended sequentially after the file containing the reference.

## Agent Definition @Imports

Agent definition files (`~/.claude/agents/*.md`) do NOT have @imports resolved.

**Agent definition file:**
```markdown
## Imports & References

- Python Standards: @~/.config/nix/config/claude/standards-python.md
- Python Extended: @~/.config/nix/config/claude/standards-python-extended.md
```

**In API request system prompt (verbatim):**
```
## Imports & References

- Python Standards: @~/.config/nix/config/claude/standards-python.md
- Python Extended: @~/.config/nix/config/claude/standards-python-extended.md
```

The @imports remain as literal text - Claude sees path references but NOT file contents.

## Resolution Summary

| Location | @Import Resolved | Content Format |
|----------|------------------|----------------|
| User prompt `-p "@file"` | Yes | Read tool call + result with line numbers |
| User CLAUDE.md `@file` | Yes | `Contents of /path (description):\n\n[content]` |
| Project CLAUDE.md `@file` | Yes | `Contents of /path (description):\n\n[content]` |
| Agent definition `@file` | No | Literal `@path/to/file` text |

## cache_control Placement

```
messages[0].content[0]  <- cache_control: {type: "ephemeral"}  (Read tool call)
messages[1].content[0]  <- no cache_control                     (Read tool result)
messages[1].content[1]  <- no cache_control                     (CLAUDE.md context)
messages[1].content[2]  <- cache_control: {type: "ephemeral"}  (User prompt)
```
