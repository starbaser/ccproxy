{
  settings = {
    host = "127.0.0.1";
    port = 4000;
    oat_sources = {
      anthropic = {
        command = "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json";
        destinations = [ "api.anthropic.com" ];
      };
      gemini = {
        command = "jq -r '.access_token' ~/.gemini/oauth_creds.json";
        destinations = [
          "cloudcode-pa.googleapis.com"
        ];
        user_agent = "GeminiCLI";
      };
    };
    hooks = {
      inbound = [
        "ccproxy.hooks.forward_oauth"
        "ccproxy.hooks.gemini_cli_compat"
        "ccproxy.hooks.reroute_gemini"
        "ccproxy.hooks.extract_session_id"
        # Example: uncomment to work around google-gemini/gemini-cli#21691 —
        # the Gemini CLI wipes its own refresh_token during access_token
        # refresh, causing "No refresh token is set" errors after ~1hr. The
        # hook stashes the refresh_token, runs the Gemini CLI to trigger a
        # refresh, and restores the refresh_token if the CLI wipes it.
        # "ccproxy.hooks.gemini_oauth_refresh"
      ];
      outbound = [
        "ccproxy.hooks.inject_mcp_notifications"
        "ccproxy.hooks.verbose_mode"
        "ccproxy.hooks.stamp_compliance"
      ];
    };
    otel = {
      enabled = false;
      endpoint = "http://localhost:4317";
      service_name = "ccproxy";
    };
    compliance = {
      enabled = true;
      profile_path = "~/.config/ccproxy/compliance_profiles.json";
    };
    inspector = {
      port = 8083;
      cert_dir = "~/.config/ccproxy";
      transforms = [
        { match_host = "cloudcode-pa.googleapis.com"; mode = "passthrough"; }
        { match_path = "/v1/messages"; mode = "redirect"; dest_provider = "anthropic"; dest_host = "api.anthropic.com"; dest_path = "/v1/messages"; dest_api_key_ref = "anthropic"; }
        { match_path = "/v1internal"; mode = "redirect"; dest_provider = "gemini"; dest_host = "cloudcode-pa.googleapis.com"; dest_api_key_ref = "gemini"; }
        { match_path = "/gemini/"; mode = "redirect"; dest_provider = "gemini"; dest_host = "cloudcode-pa.googleapis.com"; dest_api_key_ref = "gemini"; }
      ];
    };
  };
}
