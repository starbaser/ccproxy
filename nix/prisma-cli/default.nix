# Build-time Prisma client generation.
#
# prisma-client-py requires `prisma generate` to produce Python client files
# (client.py, models.py, etc.) into site-packages/prisma/. In the Nix store
# this directory is read-only, so we generate at build time and overlay via
# PYTHONPATH in the wrapper script.
{
  pkgs,
  venv,
  python,
  schemaFile,
}:

let
  nodejs = pkgs.nodejs_20;
  pyVersion = python.pythonVersion;
  prismaSitePackage = "${venv}/lib/python${pyVersion}/site-packages/prisma";

  # Pre-fetch the 6 npm packages for prisma@5.17.0 using SRI hashes
  # already present in package-lock.json. No extra hash computation needed.
  prismaNodeModules = pkgs.importNpmLock.buildNodeModules {
    npmRoot = ./.;
    inherit nodejs;
    derivationArgs = {
      # npmConfigHook already passes --ignore-scripts to `npm install`,
      # but then runs `npm rebuild` which executes postinstall scripts.
      # @prisma/engines postinstall downloads the query engine binary —
      # suppress it since we only need the CLI JS files for `prisma generate`.
      npmRebuildFlags = [ "--ignore-scripts" ];
    };
  };

in
pkgs.stdenvNoCC.mkDerivation {
  pname = "ccproxy-prisma-client";
  version = "0.15.0";

  dontUnpack = true;
  nativeBuildInputs = [ nodejs pkgs.openssl ];

  buildPhase = ''
    runHook preBuild

    WORK="$TMPDIR/prisma-work"
    mkdir -p "$WORK"

    # Copy the base prisma package to a writable staging area.
    # Shell cp/chmod from Nix store inputs fails in the sandbox, so use Python
    # which creates proper independent copies with writable permissions.
    ${venv}/bin/python -c "
import shutil, os, stat
def copy_writable(src, dst):
    shutil.copy2(src, dst)
    os.chmod(dst, os.stat(dst).st_mode | stat.S_IWUSR)
shutil.copytree('${prismaSitePackage}', '$WORK/prisma', copy_function=copy_writable)
# copytree calls copystat on dirs, inheriting Nix store 555 perms — fix them
for root, dirs, _ in os.walk('$WORK/prisma'):
    for d in dirs:
        os.chmod(os.path.join(root, d), 0o755)
os.chmod('$WORK/prisma', 0o755)
"

    # Prepare a writable copy of node_modules — the Prisma CLI writes
    # engine metadata into @prisma/engines/ even during `prisma generate`.
    CACHE_DIR="$TMPDIR/prisma-cache"
    mkdir -p "$CACHE_DIR"
    cp ${./package.json} "$CACHE_DIR/package.json"
    cp -r --no-preserve=mode ${prismaNodeModules}/node_modules "$CACHE_DIR/node_modules"

    # Create a stub query engine. The Prisma CLI checks for engine binaries
    # during `generate` and tries to download them if missing. We only need
    # the CLI to proceed — the real engine is resolved at runtime via
    # PRISMA_QUERY_ENGINE_BINARY or the user's ~/.cache/prisma-python/.
    ENGINES_DIR="$TMPDIR/engines"
    mkdir -p "$ENGINES_DIR"
    printf '#!/bin/sh\necho "query-engine 393aa359c9ad4a4bb28630fb5613f9c281cde053"\n' \
      > "$ENGINES_DIR/query-engine"
    chmod +x "$ENGINES_DIR/query-engine"
    cp "$ENGINES_DIR/query-engine" "$ENGINES_DIR/schema-engine"

    # PYTHONPATH: staging dir first so BASE_PACKAGE_DIR resolves to the
    # writable copy. The generator then writes directly into $WORK/prisma
    # without triggering copy_tree (is_same_path check passes).
    export HOME="$TMPDIR"
    export PRISMA_BINARY_CACHE_DIR="$CACHE_DIR"
    export PRISMA_QUERY_ENGINE_BINARY="$ENGINES_DIR/query-engine"
    export PRISMA_SCHEMA_ENGINE_BINARY="$ENGINES_DIR/schema-engine"
    export PRISMA_USE_GLOBAL_NODE=true
    export PRISMA_USE_NODEJS_BIN=false
    export DATABASE_URL="postgresql://localhost/dummy"
    export PYTHONPATH="$WORK:${venv}/lib/python${pyVersion}/site-packages"
    export PATH="${venv}/bin:$PATH"

    ${venv}/bin/python -m prisma generate --schema ${schemaFile}

    runHook postBuild
  '';

  installPhase = ''
    runHook preInstall
    mkdir -p "$out/lib/python${pyVersion}/site-packages"
    cp -r "$WORK/prisma" "$out/lib/python${pyVersion}/site-packages/prisma"
    runHook postInstall
  '';
}
