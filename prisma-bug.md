# Prisma client generation fails on NixOS — libssl detection

## Problem

`ccproxy start --mitm` fails to generate the Prisma client because `prisma-client-py` cannot detect the OpenSSL/libssl version. MITM traces are not persisted as a result.

```
ccproxy.mitm.process - ERROR - Prisma generate failed: prisma:warn Prisma failed to detect the libssl/openssl version to use, and m
ccproxy.mitm.process - WARNING - Prisma client generation failed - traces will not be persisted
```

## Cause

NixOS does not install libraries to standard paths (`/usr/lib`, `/lib`). Prisma's detection reads `/etc/os-release` and probes standard library directories — neither works on NixOS. Libraries live in `/nix/store/<hash>-openssl-<version>/lib/`.

The system NixOS config already sets `PRISMA_SCHEMA_ENGINE_BINARY`, `PRISMA_QUERY_ENGINE_BINARY`, `PRISMA_QUERY_ENGINE_LIBRARY`, and `PRISMA_FMT_BINARY` for the Node.js Prisma engines, but `prisma-client-py` has its own OpenSSL detection path that ignores these.

## Fix options (original proposals)

1. **Set `PRISMA_OPENSSL_LIBRARY`** in ccproxy's startup code to point at the system OpenSSL (e.g. detect via `ldconfig -p` or `pkg-config`)
2. **Detect NixOS** and use `nix eval nixpkgs#openssl.out --raw` to locate the library at runtime
3. **Accept an env var** like `CCPROXY_OPENSSL_PATH` and pass it through to Prisma's environment during `prisma generate`

## Resolution

**Status**: No code change needed — existing NixOS config is the canonical fix.

### Findings

The libssl warning is **cosmetic**, not a functional failure. Prisma's platform detection probes `/lib`, `/usr/lib`, etc. for `libssl.so.*` to determine a binary target string. On NixOS those paths don't exist, so the probe fails and the warning fires. However, when the four engine path env vars are set (`PRISMA_QUERY_ENGINE_LIBRARY`, `PRISMA_QUERY_ENGINE_BINARY`, `PRISMA_SCHEMA_ENGINE_BINARY`, `PRISMA_FMT_BINARY`), Prisma skips downloading engines entirely and uses the nix-store binaries, which have correct RPATHs baked in. The detection warning becomes irrelevant noise.

The original fix options are all invalid:

- **Option 1**: `PRISMA_OPENSSL_LIBRARY` does not exist. Prisma explicitly rejected adding an OpenSSL path override (PR #18012 closed). `ldconfig -p` returns nothing on NixOS.
- **Option 2**: `nix eval` would locate OpenSSL, but there's nothing to pass it to — no env var accepts it.
- **Option 3**: Same issue — no downstream consumer for the path.
- **`LD_LIBRARY_PATH`**: Added as a secondary fallback in Prisma v5.1.0 (PR #20381), but unnecessary when engine path vars are set. Not the recommended approach.

### Suppressing the warning

Add to `~/.config/nixos/home/tools/packages.nix` session variables:

```nix
PRISMA_DISABLE_WARNINGS = "1";
```

### Version mismatch concern

`prisma-engines_6` in nixpkgs resolves to **6.19.1**, but `prisma-client-py` 0.15.0 bundles Prisma CLI **5.17.0**. Generate succeeds because modern Prisma uses Wasm for schema generation. Runtime query engine compatibility between v6 engines and v5 client is uncertain — monitor for query-time failures.

### If this error returns

If `ensure_prisma_client()` hard-fails again (non-zero exit), the cause is likely:

1. Engine path env vars not reaching the subprocess (e.g. started outside user session)
2. Version mismatch between `prisma-engines` and `prisma-client-py` causing validation failure
3. A `prisma-client-py` update changing engine resolution behavior

Live test (2026-03-19) confirms `prisma generate` succeeds on this system with the current config.
