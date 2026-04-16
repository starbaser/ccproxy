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
        python = pkgs.python313;

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
          # Suppress uv's "Ignoring invalid SSL_CERT_FILE" warning: stdenv sets
          # SSL_CERT_FILE=/no-cert-file.crt to block network access; uv warns on
          # the missing path even though the install is --offline --no-cache.
          claude-ccproxy = prev.claude-ccproxy.overrideAttrs (old: {
            preInstall = (old.preInstall or "") + ''
              export SSL_CERT_FILE="${pkgs.cacert}/etc/ssl/certs/ca-bundle.crt"
            '';
          });
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

        mkConfig =
          {
            settings ? defaultSettings.settings,
            configDir ? ".ccproxy",
          }:
          let
            ccproxyYaml = yaml.generate "ccproxy.yaml" { ccproxy = settings; };
          in
          {
            inherit ccproxyYaml;

            shellHook = ''
              mkdir -p "${configDir}"
              ln -sfn ${ccproxyYaml} "${configDir}/ccproxy.yaml"
              export CCPROXY_CONFIG_DIR="$PWD/${configDir}"
            '';
          };

        devConfig = mkConfig {
          settings = defaultSettings.settings // {
            port = 4001;
            inspector = defaultSettings.settings.inspector // {
              port = 8084;
              cert_dir = "./.ccproxy";
              mitmproxy = {
                web_password = {
                  command = "opc secret op://dev/ccproxy/web_password";
                };
              };
            };
          };
        };
        inspectDeps = pkgs.lib.makeBinPath [
          pkgs.slirp4netns
          pkgs.wireguard-tools
          pkgs.iproute2
          pkgs.iptables
        ];
      in {
        packages = {
          default = pkgs.writeShellScriptBin "ccproxy" ''
            export PATH="${venv}/bin:${inspectDeps}:$PATH"
            exec ${venv}/bin/ccproxy "$@"
          '';
        };

        devShells = {
          default = pkgs.mkShell {
            packages = with pkgs; [
              python313
              uv
              ruff
              mypy
              pyright
              jq
              git
              just
              process-compose
              slirp4netns
              wireguard-tools
              iproute2
              iptables
            ];

            shellHook = ''
              ${devConfig.shellHook}
              export LD_LIBRARY_PATH="${pkgs.lib.makeLibraryPath [
                pkgs.stdenv.cc.cc.lib
              ]}''${LD_LIBRARY_PATH:+:$LD_LIBRARY_PATH}"
              uv sync --quiet 2>/dev/null || true
              export VIRTUAL_ENV="$PWD/.venv"
              export PATH="$PWD/.venv/bin:$PATH"
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

      inherit defaultSettings;
      homeModules.ccproxy = import ./nix/module.nix;
    };
}
