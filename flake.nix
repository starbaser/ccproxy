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
      inherit (nixpkgs) lib;
      supportedSystems = [ "x86_64-linux" "aarch64-linux" ];
      forAllSystems = f: lib.genAttrs supportedSystems f;

      workspace = uv2nix.lib.workspace.loadWorkspace { workspaceRoot = ./.; };
      overlay = workspace.mkPyprojectOverlay { sourcePreference = "wheel"; };
      defaultSettings = import ./nix/defaults.nix;

      perSystem = forAllSystems (system: let
        pkgs = nixpkgs.legacyPackages.${system};
        python = pkgs.python312;

        # Rust/C extension wheels that need autoPatchelf fixes
        wheelFixes = final: prev: {
          tokenizers = prev.tokenizers.overrideAttrs (old: {
            buildInputs = (old.buildInputs or []) ++ [ pkgs.stdenv.cc.cc.lib ];
          });
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

        prismaGenerated = pkgs.callPackage ./nix/prisma-cli {
          inherit pkgs venv python;
          schemaFile = ./prisma/schema.prisma;
        };

        yaml = pkgs.formats.yaml { };

        mkConfig =
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

        devConfig = mkConfig {
          settings = defaultSettings.settings // {
            mitm = defaultSettings.settings.mitm // {
              forward_port = 4003;
              reverse_port = 4002;
              upstream_proxy = "http://localhost:4001";
              cert_dir = "./.ccproxy";
            };
          };
          litellmSettings = defaultSettings.litellmSettings // {
            port = 4001;
          };
        };
      in {
        packages = {
          default = pkgs.writeShellScriptBin "ccproxy" ''
            export PYTHONPATH="${prismaGenerated}/lib/python${python.pythonVersion}/site-packages''${PYTHONPATH:+:$PYTHONPATH}"
            export PATH="${venv}/bin:$PATH"
            exec ${venv}/bin/ccproxy "$@"
          '';
        };

        devShells = {
          default = pkgs.mkShell {
            packages = with pkgs; [
              python312
              uv
              ruff
              mypy
              jq
              git
              just
              process-compose
              slirp4netns
              wireguard-tools
              iproute2
            ];

            shellHook = ''
              ${devConfig.shellHook}
              export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath [
                pkgs.stdenv.cc.cc.lib
              ]}''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
              uv sync --quiet 2>/dev/null || true
              export VIRTUAL_ENV="$PWD/.venv"
              export PATH="$PWD/.venv/bin:$PATH"
              export CCPROXY_PORT=4001
            '';
          };
        };

        lib = { inherit mkConfig; };
      });
    in
    {
      packages = lib.mapAttrs (_: v: v.packages) perSystem;
      devShells = lib.mapAttrs (_: v: v.devShells) perSystem;
      lib = lib.mapAttrs (_: v: v.lib) perSystem;

      homeModules.ccproxy = import ./nix/module.nix;
    };
}
