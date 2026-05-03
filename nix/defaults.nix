{
  settings = {
    host = "127.0.0.1";
    port = 4000;
    oat_sources = {
      anthropic = {
        command = "printenv CLAUDE_CODE_OAUTH_TOKEN";
        destinations = [ "api.anthropic.com" ];
      };
      gemini = {
        command = "jq -r '.access_token' ~/.gemini/oauth_creds.json";
        destinations = [
          "cloudcode-pa.googleapis.com"
        ];
        user_agent = "GeminiCLI";
      };
      deepseek = {
        command = "printenv DEEPSEEK_API_KEY";
        destinations = [ "api.deepseek.com" ];
        auth_header = "x-api-key";
      };
    };
    hooks = {
      inbound = [
        "ccproxy.hooks.forward_oauth"
        "ccproxy.hooks.extract_session_id"
      ];
      outbound = [
        "ccproxy.hooks.gemini_cli"
        {
          hook = "ccproxy.hooks.gemini_capacity_fallback";
          params = {
            fallback_models = [ "gemini-3-flash-preview" "gemini-2.5-pro" "gemini-2.5-flash" ];
          };
        }
        "ccproxy.hooks.inject_mcp_notifications"
        "ccproxy.hooks.verbose_mode"
        "ccproxy.hooks.shape"
        "ccproxy.hooks.commitbee_compat"
      ];
    };
    otel = {
      enabled = false;
      endpoint = "http://localhost:4317";
      service_name = "ccproxy";
    };
    shaping = {
      enabled = true;
      shapes_dir = "~/.config/ccproxy/shaping/shapes";
      providers = {
        anthropic = {
          content_fields = [
            "model" "messages" "tools" "tool_choice" "system" "thinking" "context_management"
            "stream" "max_tokens" "temperature" "top_p" "top_k" "stop_sequences"
          ];
          merge_strategies = { system = "prepend_shape:2"; };
          shape_hooks = [
            "ccproxy.shaping.regenerate"
            {
              hook = "ccproxy.shaping.caching.strip";
              params = { paths = [ "system.*.cache_control" ]; };
            }
            {
              hook = "ccproxy.shaping.caching.insert";
              params = {
                path = "system.-1.cache_control";
                value = { type = "ephemeral"; };
              };
            }
          ];
          preserve_headers = [ "authorization" "x-api-key" "x-goog-api-key" "host" ];
          strip_headers = [
            "authorization" "x-api-key" "x-goog-api-key"
            "content-length" "host" "transfer-encoding" "connection"
            "accept-encoding"
          ];
          capture = { path_pattern = "^/v1/messages"; };
        };
        gemini = {
          content_fields = [ "model" "project" ];
          shape_hooks = [
            "ccproxy.shaping.regenerate"
            "ccproxy.shaping.gemini"
          ];
          preserve_headers = [ "authorization" "host" ];
          strip_headers = [
            "authorization" "content-length" "host"
            "transfer-encoding" "connection" "accept-encoding"
          ];
          capture = { path_pattern = "^/v1internal:"; };
        };
      };
    };
    inspector = {
      port = 8083;
      cert_dir = "~/.config/ccproxy";
      transforms = [
        { match_host = "cloudcode-pa.googleapis.com"; mode = "passthrough"; }
        { match_path = "/v1/messages"; match_model = "deepseek-v4"; mode = "redirect"; dest_provider = "anthropic"; dest_host = "api.deepseek.com"; dest_path = "/anthropic/v1/messages"; dest_api_key_ref = "deepseek"; }
        { match_path = "/v1/messages"; mode = "redirect"; dest_provider = "anthropic"; dest_host = "api.anthropic.com"; dest_path = "/v1/messages"; dest_api_key_ref = "anthropic"; }
        { match_path = "/v1internal"; mode = "redirect"; dest_provider = "gemini"; dest_host = "cloudcode-pa.googleapis.com"; dest_api_key_ref = "gemini"; }
        { match_path = "/gemini/"; mode = "redirect"; dest_provider = "gemini"; dest_host = "cloudcode-pa.googleapis.com"; dest_api_key_ref = "gemini"; }
      ];
    };
  };
}
