# Plan: Add Compliance User-Agent for Anthropic Requests

## Task
Set user agent `claude-code/<version>` for ALL requests to the Anthropic provider.

Example: `claude-code/2.1.5`

---

## Implementation

**File**: `internal/agent/coordinator.go`

**Location**: `buildProvider()` function (~line 713-725) where headers are assembled

**Change**: When provider type is Anthropic, add User-Agent header:

```go
// Around line 713-725, after cloning ExtraHeaders
if p.Type == catwalk.TypeAnthropic {
    headers["User-Agent"] = "claude-code/" + version.Version
}
```

**Import**: Add `github.com/charmbracelet/crush/internal/version` if not present

---

## Verification

1. Build: `go build ./...`
2. Test API call with debug logging or network inspection to verify User-Agent header

---

## Critical Files

- `internal/agent/coordinator.go:713-725` - Add User-Agent header
- `internal/version/version.go` - Version constant (verify format)

---

# ARCHIVED: Previous Plan (Claude Code Support)

## Overview

Re-implement Claude Code support that was removed in PR #1783 (commit `9f03ac48c6786a8f8c6272b0c818df93b12b56ec`). The removal deleted 1,078 lines across 13 files.

## Repository Structure

Two repositories need modification:

1. **crush** (main repo) - OAuth implementation, TUI components, CLI
2. **catwalk** (submodule at `./catwalk`) - Provider database and model metadata

---

## Component Analysis

### Removed Files (crush)

| File | Lines | Purpose |
|------|-------|---------|
| `internal/oauth/claude/challenge.go` | 28 | PKCE challenge generation |
| `internal/oauth/claude/oauth.go` | 126 | OAuth2 device flow |
| `internal/tui/components/dialogs/claude/method.go` | 115 | Login method selection UI |
| `internal/tui/components/dialogs/claude/oauth.go` | 267 | Device flow TUI component |

### Modified Files (crush)

| File | Changes | Impact |
|------|---------|--------|
| `internal/config/config.go` | -22/+19 | Token refresh logic |
| `internal/config/load.go` | -5/+6 | Provider initialization |
| `internal/cmd/login.go` | -64/+1 | CLI login command |
| `internal/agent/agent.go` | -18 | Import cleanup |
| `internal/agent/coordinator.go` | -4/+4 | Import cleanup |
| `internal/tui/components/chat/splash/splash.go` | -200/+2 | Auth flow UI |
| `internal/tui/components/dialogs/models/models.go` | -122 | Model selection dialog |
| `internal/tui/components/dialogs/models/keys.go` | -57 | Import cleanup |
| `internal/tui/page/chat/chat.go` | -50/+2 | Message routing |

### Catwalk Additions

| File | Purpose |
|------|---------|
| `pkg/catwalk/provider.go` | Add `InferenceProviderClaudeCode` constant |
| `internal/providers/configs/claudecode.json` | Provider config with models |
| `internal/providers/providers.go` | Register provider |

---

## Dependency Graph

```
                    TIER 1 (Parallel)
    ┌──────────────────────────────────────────────┐
    │                                              │
    │  ┌─────────────────┐    ┌─────────────────┐  │
    │  │  catwalk        │    │  oauth/claude   │  │
    │  │  - provider.go  │    │  - challenge.go │  │
    │  │  - claudecode   │    │  - oauth.go     │  │
    │  │    .json        │    │                 │  │
    │  └────────┬────────┘    └────────┬────────┘  │
    │           │                      │           │
    └───────────┼──────────────────────┼───────────┘
                │                      │
                ▼                      ▼
                    TIER 2 (Sequential)
    ┌──────────────────────────────────────────────┐
    │                                              │
    │  ┌─────────────────┐    ┌─────────────────┐  │
    │  │  config/        │    │  cmd/login.go   │  │
    │  │  - config.go    │    │  loginClaude()  │  │
    │  │  - load.go      │    │                 │  │
    │  └────────┬────────┘    └────────┬────────┘  │
    │           │                      │           │
    └───────────┼──────────────────────┼───────────┘
                │                      │
                ▼                      ▼
                    TIER 3 (Parallel)
    ┌──────────────────────────────────────────────┐
    │                                              │
    │  ┌───────────────────────────────────────┐   │
    │  │  TUI Components                       │   │
    │  │  - dialogs/claude/oauth.go            │   │
    │  │  - dialogs/claude/method.go           │   │
    │  │  - splash/splash.go                   │   │
    │  │  - dialogs/models/models.go           │   │
    │  │  - page/chat/chat.go                  │   │
    │  └───────────────────────────────────────┘   │
    │                                              │
    └──────────────────────────────────────────────┘
```

---

## Implementation Plan

### Phase 0: Setup

1. Add catwalk as submodule at `./catwalk`
2. Update go.mod to use local replace directive
3. Verify build works

### Phase 1: Foundation (Parallel)

**Workstream A: Catwalk Provider**
- Add `InferenceProviderClaudeCode` constant to `pkg/catwalk/provider.go`
- Add to `KnownProviders()` function
- Create `internal/providers/configs/claudecode.json`
- Register in `internal/providers/providers.go`

**Workstream B: OAuth Backend**
- Create `internal/oauth/claude/challenge.go` (PKCE utility)
- Create `internal/oauth/claude/oauth.go` (device flow)
- Reference: `internal/oauth/copilot/oauth.go` for pattern

### Phase 2: Core Integration

- Update `internal/config/config.go` - add Claude case to `RefreshOAuthToken()`
- Update `internal/config/load.go` - add Claude provider init
- Implement `loginClaude()` in `internal/cmd/login.go`
- Add "claude" to ValidArgs

### Phase 3: TUI Components (Parallel sub-tasks)

- Create `internal/tui/components/dialogs/claude/oauth.go`
- Create `internal/tui/components/dialogs/claude/method.go`
- Update `splash/splash.go` - add device flow state and handlers
- Update `dialogs/models/models.go` - add Claude provider case
- Update `page/chat/chat.go` - add message routing

### Phase 4: Cleanup

- Update imports in `internal/agent/agent.go`
- Update imports in `internal/agent/coordinator.go`
- Update imports in `dialogs/models/keys.go`

---

## Parallelization Strategy

```
Time →

Agent 1 (catwalk):     [====Phase 1A====]
Agent 2 (oauth):       [====Phase 1B====]
                                         ↓
Agent 3 (config/cli):                    [==Phase 2==]
                                                      ↓
Agent 4 (TUI dialogs): ─────────────────────────────[===Phase 3===]
Agent 5 (TUI splash):  ─────────────────────────────[===Phase 3===]
Agent 6 (TUI models):  ─────────────────────────────[===Phase 3===]
```

---

## Verification

1. Build: `go build ./...`
2. Unit tests: `go test ./...`
3. CLI login: `crush login claude`
4. TUI flow: Select Claude provider in model selection
5. Token refresh: Verify expired token triggers refresh

---

## Critical Files

**catwalk (submodule):**
- `pkg/catwalk/provider.go`
- `internal/providers/configs/claudecode.json`
- `internal/providers/providers.go`

**crush:**
- `internal/oauth/claude/challenge.go` (new)
- `internal/oauth/claude/oauth.go` (new)
- `internal/tui/components/dialogs/claude/method.go` (new)
- `internal/tui/components/dialogs/claude/oauth.go` (new)
- `internal/config/config.go`
- `internal/cmd/login.go`
- `internal/tui/components/chat/splash/splash.go`
- `internal/tui/components/dialogs/models/models.go`

---

---

## Detailed Implementation

### Phase 0: Submodule Setup

```bash
# Add catwalk as submodule
git submodule add https://github.com/charmbracelet/catwalk ./catwalk

# Update go.mod to use local replace
# go.mod addition:
replace github.com/charmbracelet/catwalk => ./catwalk
```

### Phase 1A: Catwalk Provider

**File: `catwalk/pkg/catwalk/provider.go`**
```go
// Add constant (after InferenceProviderCopilot)
InferenceProviderClaudeCode InferenceProvider = "claude-code"

// Add to KnownProviders() slice
func KnownProviders() []InferenceProvider {
    return []InferenceProvider{
        // ... existing ...
        InferenceProviderClaudeCode,
    }
}
```

**File: `catwalk/internal/providers/configs/claudecode.json`**
```json
{
  "name": "Claude Code",
  "id": "claude-code",
  "type": "anthropic",
  "api_key": "$ANTHROPIC_API_KEY",
  "api_endpoint": "$ANTHROPIC_API_ENDPOINT",
  "default_large_model_id": "claude-sonnet-4-5-20250929",
  "default_small_model_id": "claude-3-5-haiku-20241022",
  "models": [
    {"id": "claude-sonnet-4-5-20250929", "name": "Claude Sonnet 4.5", ...},
    {"id": "claude-opus-4-5-20251101", "name": "Claude Opus 4.5", ...},
    {"id": "claude-haiku-4-5-20251001", "name": "Claude 4.5 Haiku", ...},
    {"id": "claude-3-5-haiku-20241022", "name": "Claude 3.5 Haiku", ...}
  ]
}
```

**File: `catwalk/internal/providers/providers.go`**
```go
//go:embed configs/claudecode.json
var claudeCodeConfig []byte

func claudeCodeProvider() catwalk.Provider {
    return loadProviderFromConfig(claudeCodeConfig)
}

// Add to providerRegistry
var providerRegistry = []ProviderFunc{
    // ... existing ...
    claudeCodeProvider,
}
```

### Phase 1B: OAuth Backend (PKCE Flow)

**File: `internal/oauth/claude/challenge.go`** (28 lines)
```go
package claude

import (
    "crypto/rand"
    "crypto/sha256"
    "encoding/base64"
    "strings"
)

func GetChallenge() (verifier, challenge string, err error) {
    bytes := make([]byte, 32)
    if _, err := rand.Read(bytes); err != nil {
        return "", "", err
    }
    verifier = encodeBase64(bytes)
    hash := sha256.Sum256([]byte(verifier))
    challenge = encodeBase64(hash[:])
    return verifier, challenge, nil
}

func encodeBase64(input []byte) string {
    encoded := base64.StdEncoding.EncodeToString(input)
    encoded = strings.ReplaceAll(encoded, "=", "")
    encoded = strings.ReplaceAll(encoded, "+", "-")
    encoded = strings.ReplaceAll(encoded, "/", "_")
    return encoded
}
```

**File: `internal/oauth/claude/oauth.go`** (126 lines)
```go
package claude

const clientId = "9d1c250a-e61b-44d9-88ed-5944d1962f5e"

// AuthorizeURL returns the OAuth2 authorization URL with PKCE challenge
func AuthorizeURL(verifier, challenge string) (string, error) {
    u, _ := url.Parse("https://claude.ai/oauth/authorize")
    q := u.Query()
    q.Set("response_type", "code")
    q.Set("client_id", clientId)
    q.Set("redirect_uri", "https://console.anthropic.com/oauth/code/callback")
    q.Set("scope", "org:create_api_key user:profile user:inference")
    q.Set("code_challenge", challenge)
    q.Set("code_challenge_method", "S256")
    q.Set("state", verifier)
    u.RawQuery = q.Encode()
    return u.String(), nil
}

// ExchangeToken exchanges authorization code for token
func ExchangeToken(ctx context.Context, code, verifier string) (*oauth.Token, error)

// RefreshToken refreshes OAuth token
func RefreshToken(ctx context.Context, refreshToken string) (*oauth.Token, error)
```

### Phase 2: Config & CLI Integration

**File: `internal/config/config.go`**
```go
// Add to RefreshOAuthToken() switch (~line 541)
case "anthropic", "claude", "claude-code":
    newToken, refreshErr = claude.RefreshToken(ctx, providerConfig.OAuthToken.RefreshToken)
```

**File: `internal/cmd/login.go`**
```go
// Add to ValidArgs
"claude", "claude-code",

// Add switch case
case "claude", "claude-code":
    return loginClaude()

// Implement loginClaude() function
func loginClaude() error {
    verifier, challenge, _ := claude.GetChallenge()
    authURL, _ := claude.AuthorizeURL(verifier, challenge)

    fmt.Println("Open this URL:", authURL)
    fmt.Print("Paste authorization code: ")

    var code string
    fmt.Scanln(&code)

    token, _ := claude.ExchangeToken(context.Background(), code, verifier)
    // Save token to config
}
```

### Phase 3: TUI Components

**File: `internal/tui/components/dialogs/claude/oauth.go`** (267 lines)
- Device flow component following Copilot pattern
- States: Display → Success/Error
- Key bindings: Enter (copy+open), C (copy), Esc (cancel)

**File: `internal/tui/components/dialogs/claude/method.go`** (115 lines)
- Login method selection (OAuth vs API key)

**Modified files:**
- `splash/splash.go` - Add device flow state, message handlers
- `dialogs/models/models.go` - Add Claude provider case
- `page/chat/chat.go` - Add message routing

---

## OAuth Flow Comparison

| Aspect | Copilot (Device Flow) | Claude (PKCE Flow) |
|--------|----------------------|-------------------|
| User action | Copy code, visit URL | Visit URL, paste code |
| Polling | Yes (background) | No |
| Complexity | Higher | Lower |
| UX | More automated | Manual code paste |

---

## Decisions Made

- **OAuth Flow**: PKCE (Authorization Code with manual code paste)
- **Approach**: Exact reversal of PR #1783, noting any deviations due to codebase evolution
- **Submodule Location**: `./catwalk`
- **Provider ID**: `claude-code` (distinct from existing `anthropic` provider)

---

## Execution Plan

### Parallel Workstreams (Phase 1)

**Agent A: Catwalk Changes**
1. Add `InferenceProviderClaudeCode` constant to `pkg/catwalk/provider.go`
2. Add to `KnownProviders()` function
3. Create `internal/providers/configs/claudecode.json` with model definitions
4. Register in `internal/providers/providers.go`

**Agent B: OAuth Backend**
1. Create `internal/oauth/claude/challenge.go` (PKCE utility)
2. Create `internal/oauth/claude/oauth.go` (authorization URL, token exchange, refresh)

### Sequential Phase 2

**After Phase 1 Complete:**
1. Update `internal/config/config.go` - add Claude refresh case
2. Update `internal/config/load.go` - add provider initialization
3. Implement `loginClaude()` in `internal/cmd/login.go`

### Parallel Phase 3 (TUI)

**Agent C: Dialog Components**
1. Create `internal/tui/components/dialogs/claude/oauth.go`
2. Create `internal/tui/components/dialogs/claude/method.go`

**Agent D: Integration Points**
1. Update `splash/splash.go` - device flow state and handlers
2. Update `dialogs/models/models.go` - Claude provider case
3. Update `page/chat/chat.go` - message routing

### Phase 4: Cleanup & Test
1. Update imports in agent/coordinator files
2. Build verification: `go build ./...`
3. Test CLI: `crush login claude`
4. Test TUI: Provider selection flow

---

## Verification Checklist

- [ ] `go build ./...` succeeds
- [ ] `go test ./...` passes
- [ ] `crush login claude` initiates PKCE flow
- [ ] TUI shows Claude Code in provider list
- [ ] OAuth token saved to config
- [ ] Token refresh works on expiry

---

## Notes on Deviations

Any differences from the original PR #1783 implementation will be documented here during implementation:

(To be filled during execution)
