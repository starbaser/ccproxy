let
  perplexityModels = builtins.fromJSON (builtins.readFile ../src/ccproxy/specs/perplexity_models.json);
  perplexityModelBindings = map (model: {
    model_name = model.id;
    litellm_params.model = "perplexity_pro/${model.id}";
  }) perplexityModels;
in
{
  settings = {
    host = "127.0.0.1";
    port = 4000;
    log_level = "INFO";
    provider_max_connections = 256;
    providers = {
      anthropic = {
        auth = {
          type = "anthropic_oauth";
          file_path = "~/.claude/.credentials.json";
          access_path = "claudeAiOauth.accessToken";
          refresh_path = "claudeAiOauth.refreshToken";
          expiry_path = "claudeAiOauth.expiresAt";
        };
        base_url = "https://api.anthropic.com";
        path = "/v1/messages";
        type = "anthropic";
      };
      gemini = {
        auth = {
          type = "google_oauth";
          client_id = "681255809395-oo8ft2oprdrnp9e3aqf6av3hmdib135j.apps.googleusercontent.com";
          client_secret = "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";
        };
        base_url = "https://cloudcode-pa.googleapis.com";
        path = "/v1internal:{action}";
        type = "gemini";
      };
      codex = {
        auth = {
          type = "codex_oauth";
        };
        base_url = "https://chatgpt.com";
        path = "/backend-api/codex/responses";
        type = "openai_responses";
      };
      deepseek = {
        auth = {
          type = "command";
          command = "printenv DEEPSEEK_API_KEY";
          header = "x-api-key";
        };
        base_url = "https://api.deepseek.com";
        path = "/anthropic/v1/messages";
        type = "anthropic";
      };
      perplexity_pro = {
        auth = {
          type = "file";
          file = "~/.opnix/secrets/perplexity-pro-api-key";
        };
        base_url = "https://www.perplexity.ai";
        path = "/rest/sse/perplexity_ask";
        type = "perplexity_pro";
        fingerprint_profile = "chrome131";
      };
      # ChatGPT consumer web session (no API key): bearer JWT + Sentinel PoW +
      # conduit prepare + browser TLS fingerprint. Opt-in like gemini — the
      # sentinel key sk-ant-oat-ccproxy-openai_conversations routes here, but the
      # provider only works once the user supplies the credential + cookie files
      # (cf_clearance is re-exported per session; see docs).
      openai_conversations = {
        auth = {
          type = "openai_conversations";
          file_path = "~/.config/ccproxy/openai-conversations-credentials.json";
          cookie_file = "~/.config/ccproxy/openai-conversations-cookies.txt";
        };
        base_url = "https://chatgpt.com";
        path = "/backend-api/f/conversation";
        type = "openai_conversations";
        fingerprint_profile = "chrome136";
      };
    };
    hooks = {
      inbound = [
        "ccproxy.hooks.inject_auth"
        "ccproxy.hooks.extract_session_id"
        "ccproxy.hooks.extract_pplx_files"
        "ccproxy.hooks.pplx_thread_inject"
      ];
      outbound = [
        "ccproxy.hooks.gemini_cli"
        "ccproxy.hooks.pplx_stamp_headers"
        "ccproxy.hooks.pplx_preflight"
        "ccproxy.hooks.inject_mcp_notifications"
        "ccproxy.hooks.verbose_mode"
        "ccproxy.hooks.commitbee_compat"
        "ccproxy.hooks.shape"
      ];
    };
    pplx = {
      search = {
        language = "en-US";
        timezone = "America/Los_Angeles";
        search_focus = "internet";
        sources = [ "web" ];
        search_recency_filter = null;
        is_incognito = false;
        skip_search_enabled = true;
        is_nav_suggestions_disabled = true;
        always_search_override = false;
        override_no_search = false;
        preflight_timeout_seconds = 5;
      };
      thread = {
        consistency_mode = "warn";
        citation_mode = "markdown";
        ttl_seconds = 1800;
        fetch_page_size = 100;
        fetch_timeout_seconds = 10;
      };
      upload = {
        max_files = 30;
        max_file_size_bytes = 52428800;
        fetch_timeout_seconds = 10;
        upload_timeout_seconds = 60;
        subscribe_timeout_seconds = 120;
      };
    };
    gemini_capacity = {
      enabled = true;
      retry_status_codes = [
        429
        503
        500
      ];
      fallback_models = [
        "gemini-3-flash-preview"
        "gemini-2.5-pro"
        "gemini-2.5-flash"
      ];
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
    mcp = {
      http = {
        enabled = true;
        host = "127.0.0.1";
        port = 4030;
        auth = null;
      };
      buffer = {
        max_events_per_task = 65536;
        ttl_seconds = 600;
      };
    };
    auth = {
      command_timeout_seconds = 5;
      refresh_timeout_seconds = 15;
      refresh_headroom_seconds = 60;
    };
    lightllm = {
      transforms = [ ];
    };
    shaping = {
      enabled = true;
      shapes_dir = "~/.config/ccproxy/shapes";
      providers = {
        anthropic = {
          content_fields = [
            "model"
            "messages"
            "tools"
            "tool_choice"
            "system"
            "thinking"
            "context_management"
            "stream"
            "max_tokens"
            "temperature"
            "top_p"
            "top_k"
            "stop_sequences"
            "diagnostics"
            "metadata"
          ];
          merge_strategies = {
            system = "prepend_shape:2";
          };
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
          preserve_headers = [
            "authorization"
            "x-api-key"
            "x-goog-api-key"
            "host"
          ];
          strip_headers = [
            "authorization"
            "x-api-key"
            "x-goog-api-key"
            "content-length"
            "host"
            "transfer-encoding"
            "connection"
            "accept-encoding"
          ];
          capture = {
            path_pattern = "^/v1/messages";
          };
        };
        gemini = {
          content_fields = [
            "model"
            "project"
            "user_prompt_id"
          ];
          shape_hooks = [
            "ccproxy.shaping.regenerate"
            "ccproxy.shaping.gemini"
          ];
          preserve_headers = [
            "authorization"
            "host"
          ];
          strip_headers = [
            "authorization"
            "content-length"
            "host"
            "transfer-encoding"
            "connection"
            "accept-encoding"
          ];
          capture = {
            path_pattern = "^/v1internal:";
          };
        };
        openai_responses = {
          content_fields = [
            "model"
            "input"
            "tools"
            "tool_choice"
            "parallel_tool_calls"
            "reasoning"
            "text"
            "stream"
            "max_output_tokens"
            "temperature"
            "top_p"
            "metadata"
            "client_metadata"
            "include"
            "previous_response_id"
            "prompt_cache_key"
            "prompt_cache_retention"
            "store"
            "truncation"
            "service_tier"
            "background"
            "safety_identifier"
            "user"
          ];
          shape_hooks = [
            "ccproxy.shaping.codex"
          ];
          preserve_headers = [
            "authorization"
            "chatgpt-account-id"
            "x-openai-fedramp"
            "host"
          ];
          strip_headers = [
            "authorization"
            "chatgpt-account-id"
            "x-openai-fedramp"
            "content-length"
            "content-encoding"
            "host"
            "transfer-encoding"
            "connection"
            "accept-encoding"
            "x-client-request-id"
            "session-id"
            "thread-id"
            "x-codex-installation-id"
            "x-codex-turn-state"
            "x-codex-turn-metadata"
            "x-codex-parent-thread-id"
            "x-codex-window-id"
            "x-openai-memgen-request"
            "x-openai-subagent"
            "openai-organization"
            "openai-project"
          ];
          capture = {
            path_pattern = "^/backend-api/codex/responses";
          };
        };
      };
    };
    inspector = {
      port = 8083;
      cert_dir = "~/.config/ccproxy";
    };
  };

  litellmConfig = {
    model_list = [
      {
        model_name = "default";
        litellm_params.model = "claude-sonnet-4-6";
      }
      {
        model_name = "claude-opus-4-6";
        litellm_params.model = "anthropic/claude-opus-4-6";
      }
      {
        model_name = "claude-sonnet-4-6";
        litellm_params.model = "anthropic/claude-sonnet-4-6";
      }
      {
        model_name = "claude-sonnet-4-5-20250929";
        litellm_params.model = "anthropic/claude-sonnet-4-5-20250929";
      }
      {
        model_name = "claude-opus-4-5-20251101";
        litellm_params.model = "anthropic/claude-opus-4-5-20251101";
      }
      {
        model_name = "claude-haiku-4-5-20251001";
        litellm_params.model = "anthropic/claude-haiku-4-5-20251001";
      }
      {
        model_name = "claude-3-5-haiku-20241022";
        litellm_params.model = "anthropic/claude-3-5-haiku-20241022";
      }
      {
        model_name = "gemini-3.1-pro-preview";
        litellm_params.model = "gemini/gemini-3.1-pro-preview";
      }
      {
        model_name = "gemini-3-pro-preview";
        litellm_params.model = "gemini/gemini-3-pro-preview";
      }
      {
        model_name = "gemini-2.5-pro";
        litellm_params.model = "gemini/gemini-2.5-pro";
      }
      {
        model_name = "gemini-3-flash-preview";
        litellm_params.model = "gemini/gemini-3-flash-preview";
      }
      {
        model_name = "gemini-3.1-flash-lite-preview";
        litellm_params.model = "gemini/gemini-3.1-flash-lite-preview";
      }
      {
        model_name = "gemini-2.5-flash";
        litellm_params.model = "gemini/gemini-2.5-flash";
      }
      {
        model_name = "gemini-2.5-flash-lite";
        litellm_params.model = "gemini/gemini-2.5-flash-lite";
      }
      {
        model_name = "gemini-2.0-flash";
        litellm_params.model = "gemini/gemini-2.0-flash";
      }
      {
        model_name = "gemini-2.0-flash-lite";
        litellm_params.model = "gemini/gemini-2.0-flash-lite";
      }
      {
        model_name = "gemini-3-pro-image-preview";
        litellm_params.model = "gemini/gemini-3-pro-image-preview";
      }
      {
        model_name = "gemini-3.1-flash-image-preview";
        litellm_params.model = "gemini/gemini-3.1-flash-image-preview";
      }
      {
        model_name = "gemini-2.5-flash-image";
        litellm_params.model = "gemini/gemini-2.5-flash-image";
      }
      {
        model_name = "deepseek-v4-pro";
        litellm_params.model = "deepseek/deepseek-v4-pro";
      }
      {
        model_name = "deepseek-v4-flash";
        litellm_params.model = "deepseek/deepseek-v4-flash";
      }
    ] ++ perplexityModelBindings;
  };
}
