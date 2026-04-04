{
  repositories = {
    litellm = {
      url = "https://github.com/BerriAI/litellm";
      kits = {
        core = {
          include = [
            "litellm/main.py"
            "litellm/utils.py"
            "litellm/router.py"
            "litellm/types/**"
            "litellm/constants.py"
            "litellm/exceptions.py"
            "litellm/timeout.py"
          ];
          chunk_by = "symbols";
        };
        docs = {
          include = [
            "docs/**/*.md"
            "docs/**/*.mdx"
            "README.md"
            "CONTRIBUTING.md"
          ];
          exclude = [
            "docs/my-website/node_modules/**"
            "docs/my-website/.next/**"
            "docs/**/*.ipynb"
            "cookbook/**/*.ipynb"
          ];
          chunk_by = "lines";
        };
        llms = {
          include = [
            "litellm/llms/**"
            "litellm/integrations/**"
          ];
          exclude = [
            "**/test*"
            "**/*.test.py"
            "tests/**"
            "litellm/llms/replicate/**"
            "litellm/llms/petals/**"
            "litellm/llms/vllm/**"
            "litellm/llms/vertex_ai/**"
            "litellm/llms/bedrock/**"
            "litellm/llms/baseten/**"
            "litellm/llms/helicone/**"
            "litellm/llms/aleph_alpha/**"
            "litellm/llms/baseten/**"
          ];
          chunk_by = "symbols";
        };
      };
    };
    "proxy/mitmproxy" = {
      url = "https://github.com/mitmproxy/mitmproxy";
      kits = {
        docs = { include = [ "docs/**" ]; chunk_by = "lines"; };
        src = {
          include = [
            "mitmproxy/proxy/**"
            "mitmproxy/net/**"
            "mitmproxy/addons/**"
            "mitmproxy/*.py"
            "examples/**"
          ];
          exclude = [
            "test/**"
            "web/**"
            "mitmproxy/tools/**"
            "release/**"
            ".github/**"
          ];
          chunk_by = "symbols";
        };
      };
    };
    slirp4netns = {
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
  };
}
