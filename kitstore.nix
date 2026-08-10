{
  use = [
    {
      node = "@repo:mitmproxy";
      mount = "inspector/mitmproxy";
    }
    {
      node = "@repo:slirp4netns";
      mount = "inspector/slirp4netns";
    }
    {
      node = "@repo:xepor";
      mount = "inspector/xepor";
    }
    {
      node = "@repo:xepor-examples";
      mount = "inspector/xepor-examples";
    }
    {
      node = "@repo:jlowin-fastmcp";
      mount = "lib/fastmcp";
    }
    {
      node = "@repo:glom";
      mount = "lib/glom";
    }
    {
      node = "@repo:pydantic-ai";
      mount = "lib/pydantic-ai";
    }
    {
      node = "@repo:tyro";
      mount = "lib/tyro";
    }
    {
      node = "@repo:ty";
      mount = "lsp/ty";
    }
    {
      node = "@repo:litellm";
      mount = "litellm";
    }
    {
      node = "@repo:plotille";
      mount = "plotille";
    }
  ];
  config = {
    auto_mount = true;
  };
}
