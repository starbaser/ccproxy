{
  repositories = {
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
  };
}
