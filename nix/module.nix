# Home Manager module for ccproxy
{ config, lib, pkgs, inputs, ... }:

let
  cfg = config.programs.ccproxy;
  defaults = import ./defaults.nix;
  yaml = pkgs.formats.yaml { };

  ccproxyYaml = yaml.generate "ccproxy.yaml" { ccproxy = cfg.settings; };
in
{
  options.programs.ccproxy = {
    enable = lib.mkEnableOption "ccproxy LLM API proxy";

    package = lib.mkOption {
      type = lib.types.package;
      default = inputs.ccproxy.packages.${pkgs.stdenv.hostPlatform.system}.default;
      description = "The ccproxy package.";
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
  };

  config = lib.mkIf cfg.enable {
    home.packages = [ cfg.package ];

    home.file."${cfg.configDir}/ccproxy.yaml".source = ccproxyYaml;

    systemd.user.services.ccproxy = {
      Unit = {
        Description = "ccproxy LLM API Proxy";
        After = [ "default.target" ];
      };
      Service = {
        Type = "simple";
        ExecStart = "${cfg.package}/bin/ccproxy start";
        Restart = "on-failure";
        RestartSec = "5s";
        SyslogIdentifier = "ccproxy";
        Environment = [
          "HOME=%h"
          "CCPROXY_CONFIG_DIR=%h/${cfg.configDir}"
        ];
      };
      Install.WantedBy = [ "default.target" ];
      Unit."X-Restart-Triggers" = [ ccproxyYaml ];
    };
  };
}
