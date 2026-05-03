{
  repositories = {
    "community/cchistory" = {
      url = "https://github.com/badlogic/cchistory";
      kits = {
        docs = { include = [ "README.md" ]; chunk_by = "lines"; };
        src = { include = [ "src/**/*.ts" ]; chunk_by = "symbols"; };
      };
    };
    "community/claude-code-reverse-engineering" = {
      url = "https://github.com/jung-wan-kim/claude-code-reverse-engineering";
      kits = {
        docs = { include = [ "docs/**" "README.md" "index.html" ]; chunk_by = "lines"; };
        infra = { include = [ "infrastructure/**" ]; chunk_by = "lines"; };
      };
    };
    "community/claude_code_re" = {
      url = "https://github.com/memaxo/claude_code_re";
      kits = {
        docs = {
          include = [
            "docs/**"
            "README.md"
            "PLAN.md"
            "ast_analysis/README.md"
            "ast_analysis/scope_report.md"
            "ast_analysis/flow_reports/*.md"
            "todo_system_implementation/README.md"
            "todo_system_implementation/PLAN.md"
            "edit_tool_implementation/PLAN.md"
          ];
          chunk_by = "lines";
        };
        src = {
          include = [
            "*.js"
            "*.py"
            "ast_analysis/*.js"
            "ast_analysis/flow_reports/*.md"
            "edit_tool_implementation/*.js"
            "todo_system_implementation/*.js"
          ];
          exclude = [
            "ast_analysis/node_modules/**"
            "ast_analysis/output/**"
            "ast_analysis/variables_map.json"
          ];
          chunk_by = "symbols";
        };
      };
    };
    "community/llm-interceptor" = {
      url = "https://github.com/chouzz/llm-interceptor";
      kits = {
        docs = {
          include = [
            "README.md"
            "CHANGELOG.md"
            "lli.example.toml"
            "ui/README.md"
          ];
          chunk_by = "lines";
        };
        src = {
          include = [
            "src/**/*.py"
            "tests/**/*.py"
            "ui/src/**/*.ts"
            "ui/src/**/*.tsx"
          ];
          chunk_by = "symbols";
        };
      };
    };
    "community/opencode-claude-auth" = {
      url = "https://github.com/griffinmartin/opencode-claude-auth";
      kits = {
        docs = {
          include = [
            "README.md"
            "installation.md"
            "CHANGELOG.md"
            "src/anthropic-prompt.txt"
          ];
          chunk_by = "lines";
        };
        src = { include = [ "src/**/*.ts" "scripts/**/*.ts" ]; chunk_by = "symbols"; };
      };
    };
    "community/opencode-claude-auth-sync" = {
      url = "https://github.com/lehdqlsl/opencode-claude-auth-sync";
      kits = {
        docs = { include = [ "README.md" "LICENSE" ]; chunk_by = "lines"; };
        src = { include = [ "*.sh" "*.ps1" ]; chunk_by = "lines"; };
      };
    };
    "community/proxyclawd" = {
      url = "https://github.com/dyshay/proxyclawd";
      kits = {
        docs = {
          include = [
            "README.md"
            "openclaw-skill/SKILL.md"
            "openclaw-skill/**/*.sh"
          ];
          chunk_by = "lines";
        };
        src = {
          include = [
            "src/**"
            "proxyclawd-mcp/src/**"
            "frontend/src/**"
            "proxyclawd-mcp/Cargo.toml"
            "Cargo.toml"
          ];
          chunk_by = "symbols";
        };
      };
    };
    "inspector/mitmproxy" = {
      url = "https://github.com/mitmproxy/mitmproxy";
      kits = {
        docs = { include = [ "docs/src/**" ]; chunk_by = "lines"; };
        src = {
          include = [
            "mitmproxy/**/*.py"
            "examples/**/*.py"
          ];
          exclude = [
            "test/**"
            "mitmproxy/test/**"
            "mitmproxy/contrib/**"
            "mitmproxy/tools/**"
            "**/test_*.py"
            "**/*_test.py"
          ];
          chunk_by = "symbols";
        };
      };
    };
    "inspector/slirp4netns" = {
      url = "https://github.com/rootless-containers/slirp4netns";
      kits = {
        docs = {
          include = [
            "README.md"
            "slirp4netns.1.md"
            "COPYING"
            "MAINTAINERS"
            "SECURITY_CONTACTS"
          ];
          chunk_by = "lines";
        };
        src = {
          include = [
            "**/*.c"
            "**/*.h"
            "Makefile.am"
            "configure.ac"
            "autogen.sh"
          ];
          exclude = [
            "tests/**"
            "vendor/**"
            "Dockerfile*"
            ".github/**"
            "benchmarks/**"
          ];
          chunk_by = "symbols";
        };
      };
    };
    "inspector/xepor" = {
      url = "https://github.com/xepor/xepor";
      kits = {
        docs = { include = [ "docs/**" ]; chunk_by = "lines"; };
        src = { include = [ "src/xepor/**" ]; chunk_by = "symbols"; };
      };
    };
    "inspector/xepor-examples" = {
      url = "https://github.com/xepor/xepor-examples";
    };
    "lib/glom" = {
      url = "https://github.com/mahmoud/glom";
      kits = {
        docs = {
          include = [
            "docs/**/*.rst"
            "docs/**/*.md"
            "README.md"
            "CHANGELOG.md"
          ];
          chunk_by = "lines";
        };
        src = { include = [ "glom/**/*.py" ]; chunk_by = "symbols"; };
      };
    };
    "lib/tyro" = {
      url = "https://github.com/brentyi/tyro";
      kits = {
        docs = {
          include = [
            "docs/source/**/*.rst"
            "docs/source/**/*.md"
            "README.md"
          ];
          chunk_by = "lines";
        };
        src = { include = [ "src/tyro/**/*.py" "examples/**/*.py" ]; chunk_by = "symbols"; };
      };
    };
    litellm = {
      url = "https://github.com/BerriAI/litellm";
      kits = {
        core = {
          include = [
            "litellm/types/**/*.py"
            "litellm/integrations/**/*.py"
            "litellm/caching/**/*.py"
            "litellm/responses/**/*.py"
            "litellm/router.py"
            "litellm/main.py"
            "litellm/__init__.py"
            "litellm/router_strategy/**/*.py"
            "litellm/router_utils/**/*.py"
            "litellm/litellm_core_utils/**/*.py"
            "litellm/secret_managers/**/*.py"
          ];
          exclude = [
            "tests/**/*"
            "litellm/integrations/SlackAlerting/**/*"
          ];
          chunk_by = "symbols";
        };
        docs = { include = [ "docs/my-website/docs/**/*.md" ]; chunk_by = "lines"; };
        llms = {
          include = [ "litellm/llms/**/*.py" ];
          exclude = [ "tests/**/*" ];
          chunk_by = "symbols";
        };
        proxy = {
          include = [ "litellm/proxy/**/*.py" ];
          exclude = [ "tests/**/*" ];
          chunk_by = "symbols";
        };
      };
    };
    pydantic = {
      url = "https://github.com/pydantic/pydantic";
      kits = {
        docs = { include = [ "docs/**/*.md" "README.md" ]; chunk_by = "lines"; };
        src = { include = [ "pydantic/**/*.py" ]; chunk_by = "symbols"; };
      };
    };
    rich = {
      url = "https://github.com/Textualize/rich";
      kits = {
        docs = {
          include = [
            "docs/source/**/*.rst"
            "docs/source/**/*.md"
            "README.md"
            "CHANGELOG.md"
          ];
          chunk_by = "lines";
        };
        src = { include = [ "rich/**/*.py" ]; chunk_by = "symbols"; };
      };
    };
    "sdk/anthropic-python" = {
      url = "https://github.com/anthropics/anthropic-sdk-python";
    };
    "sdk/google-genai-python" = {
      url = "https://github.com/googleapis/python-genai";
    };
    "sdk/openai-python" = {
      url = "https://github.com/openai/openai-python";
    };
  };

  config = {
    auto_mount = true;
  };
}
