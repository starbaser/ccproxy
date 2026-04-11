{
  settings = {
    host = "127.0.0.1";
    port = 4000;
    debug = true;
    oat_sources = {
      anthropic = {
        command = "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json";
        destinations = [ "api.anthropic.com" ];
      };
      gemini = {
        command = "jq -r '.access_token' ~/.gemini/oauth_creds.json";
        destinations = [
          "generativelanguage.googleapis.com"
          "cloudcode-pa.googleapis.com"
        ];
        user_agent = "GeminiCLI";
      };
    };
    hooks = {
      inbound = [
        "ccproxy.hooks.forward_oauth"
        "ccproxy.hooks.extract_session_id"
      ];
      outbound = [
        "ccproxy.hooks.inject_mcp_notifications"
        "ccproxy.hooks.verbose_mode"
        "ccproxy.hooks.apply_compliance"
      ];
    };
    otel = {
      enabled = false;
      endpoint = "http://localhost:4317";
      service_name = "ccproxy";
    };
    compliance = {
      enabled = true;
      min_observations = 1;
    };
    inspector = {
      port = 8083;
      cert_dir = "~/.ccproxy";
      debug = false;
      transforms = [
        { match_host = "cloudcode-pa.googleapis.com"; mode = "passthrough"; }
        { match_path = "/v1/messages"; mode = "redirect"; dest_provider = "anthropic"; dest_host = "api.anthropic.com"; dest_path = "/v1/messages"; dest_api_key_ref = "anthropic"; }
        { match_path = "/v1internal"; mode = "redirect"; dest_provider = "gemini"; dest_host = "cloudcode-pa.googleapis.com"; dest_api_key_ref = "gemini"; }
        { match_path = "/gemini/"; mode = "redirect"; dest_provider = "gemini"; dest_host = "cloudcode-pa.googleapis.com"; dest_api_key_ref = "gemini"; }
      ];
    };
  };
}
