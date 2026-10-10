# WeChatArchive

A local WeChat 4 archive for **Windows and macOS**, with one Python core and separate platform launch packages. [中文说明](README_ZH.md).

The v0.2.0 preview adds continuous authenticated capture, transactional message changes, resumable conversation/day context indexing, and current-user login services to the existing archive and viewer. Only process data you own or are authorized to access.

The same core exposes a CLI, local web viewer, authenticated read-only HTTP API and background runner. Desktop and MCP shells can build on it. See [local API and packaging](docs/local-api.md) and [continuous operation](docs/continuous.md).

## Install from source

Requires Python 3.11+. Native decryption requires Windows or macOS; existing plaintext snapshot parsing and archive viewing also run on Linux.

```sh
python -m venv .venv
# macOS: .venv/bin/python; Windows: .venv\Scripts\python.exe
python -m pip install .
python -m wechat_archive --help
```

Run commands with the virtual environment's Python (or activate it first). Portable packages contain the same wheel and either `launch.command` or `start.cmd`. **They require Python and internet access for first dependency installation; they are not native App/EXE installers and do not bundle Python.** The launcher opens the existing-archive wizard; initialization uses the explicit commands below.

```sh
python -m wechat_archive archive --source "/path/to/db_storage" --keys-json "/private/verified-keys.json" --output "/private/archive"
python -m wechat_archive serve --archive "/private/archive/archive.sqlite"
```

The viewer prints a temporary loopback URL. Open the complete URL in your browser; do not share it. Archive data is never uploaded. Stop the local server with Ctrl+C.

## Key acquisition

Existing verified keys are the preferred input. `import-keys` stores validated keys in macOS Keychain or Windows DPAPI. Key JSON accepts `{"keys":{"message/message_0.db":{"enc_key":"..."}}}` or `keys_by_salt`; it must cover the selected account's databases. Do not put real keys in source, examples or issues.

```sh
python -m wechat_archive import-keys --source "/path/to/db_storage" --input "/private/verified-keys.json" --state "/private/credentials"
python -m wechat_archive archive --source "/path/to/db_storage" --key-store "/private/credentials/keys.protected" --output "/private/archive"
```

Windows bootstrap uses the adapted read-only process-memory scanner. Adequate process access may require administrator privileges. macOS bootstrap uses a temporary copy and **never re-signs the official app**. It requires Frida and explicit restart authorization:

```sh
python -m pip install '.[mac-key]'
python -m wechat_archive bootstrap --source "/path/to/db_storage" --state "/private/credentials" --allow-wechat-restart
```

Run macOS initialization with the official `/Applications/WeChat.app` open. It copies/signs only a temporary app, quits the official instance, captures/verifies candidates, stops owned copy processes and attempts to restore the official instance. Cancellation, permission denial and app-version changes can fail. The integrated wrapper is experimental; original capture and reader cores have prior real-device evidence, while wrapper recovery is tested with synthetic fixtures. SIP is not disabled. Windows bootstrap uses the same command without the macOS restart flag.

## Continuous operation

After importing verified keys, create a private JSON configuration with absolute `source`, `output`, `key_store` and optional `context_output` paths. See the [Chinese configuration guide](docs/continuous.md). The default interval is 120 seconds; processing checkpoints survive restarts.

```sh
python -m wechat_archive watch --config "/private/runner.json" --once
python -m wechat_archive service-install --config "/private/runner.json"
python -m wechat_archive service-uninstall
```

Use the virtual environment's Python. macOS uses a user LaunchAgent; Windows uses a least-privilege logon task. The computer must be awake with the user logged in and source databases available. Services reuse protected keys and never bootstrap keys or restart WeChat. Context processing currently organizes evidence by conversation/day with local search; semantic summaries and additional sources are future adapters.

## Limits and validation

- Full local database scope includes service/system/unmapped conversations. This does not retrieve cloud or other-device history.
- Per-database consistent snapshots are not a simultaneous account-wide freeze.
- Dedicated WCDB FTS tokenizers can prevent SQLite integrity checks. Such indexes are retained and explicitly marked `extension_required`; successful page authentication is separate from successful SQLite validation.
- Media records and raw fields are retained; media bodies are not decoded in this preview.
- Native installers, cross-device merge, semantic summarization and media playback are not included.
- The public test suite uses synthetic databases, mock Keychain, a public AES vector, and an in-memory UI fixture. It does not require personal WeChat data or actually bootstrap WeChat in CI.

```sh
python -m unittest discover -s tests -q
node --test tests/ui.test.cjs
python -m pip install build 'setuptools>=77,<83'
python scripts/package.py --output dist --platform all
```

There is no real-browser UI sign-off yet. Windows and macOS CI checks validate the common core and native AES; they do not establish compatibility with every WeChat release. See [architecture](docs/architecture.md), [release scope](docs/release-scope.md), and [third-party attribution](THIRD_PARTY.md).

MIT, © Julian Cooper and contributors. Included upstream sources retain their own MIT copyright notices.
