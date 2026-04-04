# Home Manager module for ccproxy
{ config, lib, pkgs, inputs, ... }:

let
  cfg = config.programs.ccproxy;
  defaults = import ./defaults.nix;
  yaml = pkgs.formats.yaml { };

  ccproxyYaml = yaml.generate "ccproxy.yaml" (
    { ccproxy = cfg.settings; }
    // lib.optionalAttrs (cfg.litellmSettings != { }) { litellm = cfg.litellmSettings; }
  );

  litellmConfigYaml = yaml.generate "config.yaml" cfg.litellmConfig;
in
{
  options.programs.ccproxy = {
    enable = lib.mkEnableOption "ccproxy LLM API proxy";

    package = lib.mkOption {
      type = lib.types.package;
      default = inputs.ccproxy.packages.${pkgs.stdenv.hostPlatform.system}.default;
      description = "The ccproxy package.";
    };

    inspect = lib.mkOption {
      type = lib.types.bool;
      default = false;
      description = "Enable inspect mode (--inspect flag).";
    };

    configDir = lib.mkOption {
      type = lib.types.str;
      default = ".ccproxy";
      description = "Config directory relative to home.";
    };

    settings = lib.mkOption {
      type = lib.types.attrs;
      default = defaults.settings;
      description = ''
        ccproxy settings (the `ccproxy:` section of ccproxy.yaml).
        Freeform attrset — any key is accepted and serialized to YAML.
      '';
    };

    litellmSettings = lib.mkOption {
      type = lib.types.attrs;
      default = defaults.litellmSettings;
      description = ''
        LiteLLM subprocess settings (the `litellm:` section of ccproxy.yaml).
        Controls host, port, workers, and environment variables passed to the litellm process.
      '';
    };

    litellmConfig = lib.mkOption {
      type = lib.types.attrs;
      default = defaults.litellmConfig;
      description = ''
        LiteLLM proxy configuration (the entire config.yaml).
        Contains model_list, litellm_settings, router_settings, and general_settings.
      '';
    };
  };

  config = lib.mkIf cfg.enable {
    home.packages = [ cfg.package ];

    home.file."${cfg.configDir}/ccproxy.yaml".source = ccproxyYaml;
    home.file."${cfg.configDir}/config.yaml".source = litellmConfigYaml;

    systemd.user.services.ccproxy = {
      Unit = {
        Description = "ccproxy LLM API Proxy";
        After = [ "default.target" ];
      };
      Service = {
        Type = "simple";
        ExecStart = "${cfg.package}/bin/ccproxy start${lib.optionalString cfg.inspect " --inspect"}";
        Restart = "on-failure";
        RestartSec = "5s";
        SyslogIdentifier = "ccproxy";
        Environment = [
          "HOME=%h"
          "CCPROXY_CONFIG_DIR=%h/${cfg.configDir}"
        ];
      };
      Install.WantedBy = [ "default.target" ];
    };
  };
}
