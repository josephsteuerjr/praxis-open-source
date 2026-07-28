# Relay

`relay/` is an imported snapshot of the Relay source tree. It provides an
OpenAI-compatible Rust proxy around ChatGPT/Codex authentication and response
translation.

## License boundary

Everything under this directory is licensed under the **MIT License**; see
[`LICENSE`](LICENSE). The MIT notice must remain with copies or substantial
portions of Relay.

The repository outside `relay/` is Praxis and is licensed separately under
**GNU AGPL version 3**; see the repository-root [`LICENSE`](../LICENSE) and
[`LICENSE-AGPL-3.0.txt`](../LICENSE-AGPL-3.0.txt). The root AGPL license does
not replace or relicense the files in this directory.

## Snapshot provenance

The source snapshot was exported from `/opt/relay/Code` commit
`23938ba5890813d884f0733d34ee4f7c6dad3de2` using `git archive`. Its nested
Git metadata and ignored local rollback directory `src.bak-toolfix/` were not
included. The imported source is unchanged except for this README, the added
MIT license notice, and the `license = "MIT"` package metadata in `Cargo.toml`.
This is a source import, not a Git submodule.

To refresh the snapshot without creating a nested `.git` directory, export a
reviewed commit into a temporary directory, audit it, then replace the tracked
contents while retaining this README and MIT license. Do not recursively copy
the working tree.

## Build and test

```bash
cargo test --manifest-path relay/Cargo.toml --locked
```

The program performs OAuth login and stores token data at runtime. Runtime
credentials, logs and Rust build output are intentionally excluded from source
control; never commit them. See `src/core/config.rs` and `src/login/` for the
current runtime paths and authentication flow.
