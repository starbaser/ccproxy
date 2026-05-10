{
  settings = {
    host = "127.0.0.1";
    port = 4000;
    log_level = "INFO";
    providers = {
      anthropic = {
        auth = {
          type = "command";
          command = "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json";
        };
        host = "api.anthropic.com";
        path = "/v1/messages";
        provider = "anthropic";
      };
      gemini = {
        auth = {
          type = "google_oauth";
          client_id = "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com";
          client_secret = "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";
        };
        host = "cloudcode-pa.googleapis.com";
        path = "/v1internal:{action}";
        provider = "gemini";
      };
      deepseek = {
        auth = {
          type = "command";
          command = "printenv DEEPSEEK_API_KEY";
          header = "x-api-key";
        };
        host = "api.deepseek.com";
        path = "/anthropic/v1/messages";
        provider = "anthropic";
      };
      perplexity_pro = {
        auth = {
          type = "file";
          file = "~/.config/ccproxy/perplexity-session-token";
        };
        host = "www.perplexity.ai";
        path = "/rest/sse/perplexity_ask";
        provider = "perplexity_pro";
      };
    };
    hooks = {
      inbound = [
        "ccproxy.hooks.forward_oauth"
        "ccproxy.hooks.extract_session_id"
      ];
      outbound = [
        "ccproxy.hooks.gemini_cli"
        "ccproxy.hooks.inject_mcp_notifications"
        "ccproxy.hooks.verbose_mode"
        "ccproxy.hooks.commitbee_compat"
        "ccproxy.hooks.shape"
      ];
    };
    gemini_capacity = {
      enabled = true;
      retry_status_codes = [ 429 503 500 ];
      fallback_models = [ "gemini-3-flash-preview" "gemini-2.5-pro" "gemini-2.5-flash" ];
      sticky_retry_attempts = 3;
      sticky_retry_max_delay_seconds = 60;
      terminal_delay_threshold_seconds = 300;
      total_retry_budget_seconds = 120;
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
              params = {
                paths = [ "system.*.cache_control" ];
              };
            }
            {
              hook = "ccproxy.shaping.caching.insert";
              params = {
                path = "system.-1.cache_control";
                value = {
                  type = "ephemeral";
                };
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
      transforms = [];
    };
  };
}
