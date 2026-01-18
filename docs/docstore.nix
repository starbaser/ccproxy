{
  # Enable workspaces for project-specific documentation management
  workspaces = true;

  # Remote repositories fetched via Nix
  ctx = {
    litellm = {
      url = "https://github.com/BerriAI/litellm";
      include = [
        "docs/my-website/docs/**"
      ];
    };
  };
}
