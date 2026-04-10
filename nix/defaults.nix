{
  settings = {
    debug = true;
    oauth_ttl = 28800;
    oauth_refresh_buffer = 0.1;
    oat_sources = {
      anthropic = {
        command = "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json";
        destinations = [ "api.anthropic.com" ];
      };
      gemini = {
        command = "jq -r '.access_token' ~/.gemini/oauth_creds.json";
      };
    };
    hooks = {
      inbound = [
        "ccproxy.hooks.forward_oauth"
        "ccproxy.hooks.extract_session_id"
      ];
      outbound = [
        "ccproxy.hooks.add_beta_headers"
        "ccproxy.hooks.inject_claude_code_identity"
        "ccproxy.hooks.inject_mcp_notifications"
      ];
    };
    otel = {
      enabled = false;
      endpoint = "http://localhost:4317";
      service_name = "ccproxy";
    };
    inspector = {
      port = 8083;
      capture_bodies = true;
      cert_dir = "~/.ccproxy";
      debug = false;
    };
  };

  litellmSettings = {
    host = "127.0.0.1";
    port = 4000;
  };
}
