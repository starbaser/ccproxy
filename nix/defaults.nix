{
  settings = {
    debug = true;
    handler = "ccproxy.handler:CCProxyHandler";
    oauth_ttl = 28800;
    oauth_refresh_buffer = 0.1;
    oat_sources = {
      anthropic = {
        command = "jq -r '.claudeAiOauth.accessToken' ~/.claude/.credentials.json";
        destinations = [ "api.anthropic.com" ];
      };
    };
    hooks = [
      "ccproxy.hooks.rule_evaluator"
      "ccproxy.hooks.model_router"
      "ccproxy.hooks.capture_headers"
      "ccproxy.hooks.forward_oauth"
      "ccproxy.hooks.add_beta_headers"
      "ccproxy.hooks.inject_claude_code_identity"
    ];
    default_model_passthrough = true;
    rules = [ ];
    mitm = {
      enabled = false;
      port = 8081;
      upstream_proxy = "http://localhost:4000";
      database_url = "postgresql://ccproxy:\${CCPROXY_DB_PASSWORD:-test}@localhost:5433/ccproxy_mitm";
      capture_bodies = true;
      max_body_size = 0;
      excluded_hosts = [ ];
      cert_dir = "~/.ccproxy";
      debug = false;
    };
  };

  litellmSettings = {
    host = "127.0.0.1";
    port = 4000;
    num_workers = 4;
    debug = true;
    detailed_debug = true;
  };

  litellmConfig = {
    model_list = [
      {
        model_name = "default";
        litellm_params = {
          model = "claude-sonnet-4-6";
        };
      }
      {
        model_name = "claude-opus-4-6";
        litellm_params = {
          model = "anthropic/claude-opus-4-6";
          api_base = "https://api.anthropic.com";
        };
      }
      {
        model_name = "claude-sonnet-4-6";
        litellm_params = {
          model = "anthropic/claude-sonnet-4-6";
          api_base = "https://api.anthropic.com";
        };
      }
      {
        model_name = "claude-sonnet-4-5-20250929";
        litellm_params = {
          model = "anthropic/claude-sonnet-4-5-20250929";
          api_base = "https://api.anthropic.com";
        };
      }
      {
        model_name = "claude-opus-4-5-20251101";
        litellm_params = {
          model = "anthropic/claude-opus-4-5-20251101";
          api_base = "https://api.anthropic.com";
        };
      }
      {
        model_name = "claude-haiku-4-5-20251001";
        litellm_params = {
          model = "anthropic/claude-haiku-4-5-20251001";
          api_base = "https://api.anthropic.com";
        };
      }
      {
        model_name = "claude-3-5-haiku-20241022";
        litellm_params = {
          model = "anthropic/claude-3-5-haiku-20241022";
          api_base = "https://api.anthropic.com";
        };
      }
    ];
    litellm_settings = {
      force_stream = true;
      num_retries = 0;
      callbacks = [ "langfuse" "ccproxy.handler" ];
      success_callback = [ "langfuse" ];
    };
    router_settings = {
      enable_pre_call_checks = false;
      retry_after = 0;
      allowed_fails = 1000;
      cooldown_time = 0;
    };
    general_settings = {
      disable_spend_logs = true;
      forward_client_headers_to_llm_api = true;
      disable_master_key_return = true;
      max_parallel_requests = 1000000;
      global_max_parallel_requests = 1000000;
    };
  };
}
