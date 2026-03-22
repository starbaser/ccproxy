{
  inputs = {
    nixpkgs.url = "github:NixOS/nixpkgs/nixos-unstable";

    pyproject-nix = {
      url = "github:pyproject-nix/pyproject.nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    uv2nix = {
      url = "github:pyproject-nix/uv2nix";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };

    pyproject-build-systems = {
      url = "github:pyproject-nix/build-system-pkgs";
      inputs.pyproject-nix.follows = "pyproject-nix";
      inputs.uv2nix.follows = "uv2nix";
      inputs.nixpkgs.follows = "nixpkgs";
    };
  };

  outputs =
    {
      self,
      nixpkgs,
      uv2nix,
      pyproject-nix,
      pyproject-build-systems,
      ...
    }:
    let
      system = "x86_64-linux";
      pkgs = nixpkgs.legacyPackages.${system};
      inherit (nixpkgs) lib;

      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
      overlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };
      python = pkgs.python312;

      # Rust/C extension wheels that need autoPatchelf relaxation
      wheelFixes = final: prev: {
        mitmproxy-rs = prev.mitmproxy-rs.overrideAttrs {
          autoPatchelfIgnoreMissingDeps = true;
        };
        tiktoken = prev.tiktoken.overrideAttrs {
          autoPatchelfIgnoreMissingDeps = true;
        };
      };

      pythonSet =
        (pkgs.callPackage pyproject-nix.build.packages {
          inherit python;
        }).overrideScope
          (
            lib.composeManyExtensions [
              pyproject-build-systems.overlays.default
              overlay
              wheelFixes
            ]
          );

      venv = pythonSet.mkVirtualEnv "ccproxy-env" workspace.deps.default;

      yaml = pkgs.formats.yaml { };

      defaultSettings = import ./nix/defaults.nix;
    in
    {
      packages.${system}.default = pkgs.writeShellScriptBin "ccproxy" ''
        exec ${venv}/bin/ccproxy "$@"
      '';

      homeModules.ccproxy = import ./nix/module.nix;

      lib.${system}.mkConfig =
        {
          settings ? defaultSettings.settings,
          litellmSettings ? defaultSettings.litellmSettings,
          litellmConfig ? defaultSettings.litellmConfig,
          configDir ? ".ccproxy",
        }:
        let
          ccproxyYaml = yaml.generate "ccproxy.yaml" (
            { ccproxy = settings; }
            // lib.optionalAttrs (litellmSettings != { }) { litellm = litellmSettings; }
          );
          litellmConfigYaml = yaml.generate "config.yaml" litellmConfig;
        in
        {
          inherit ccproxyYaml litellmConfigYaml;

          shellHook = ''
            mkdir -p ${configDir}
            ln -sfn ${ccproxyYaml} ${configDir}/ccproxy.yaml
            ln -sfn ${litellmConfigYaml} ${configDir}/config.yaml
            export CCPROXY_CONFIG_DIR="$PWD/${configDir}"
          '';
        };

      devShells.${system}.default = pkgs.mkShell {
        packages = with pkgs; [
          python312
          uv
          ruff
          mypy
          jq
          git
        ];

        shellHook = ''
          uv sync --quiet 2>/dev/null || true
          export VIRTUAL_ENV="$PWD/.venv"
          export PATH="$PWD/.venv/bin:$PATH"
        '';
      };
    };
}
