# WeChatArchive：Windows / macOS 本地微信归档

一个开源仓库、同一套 Python 核心、两个平台启动包。v0.1.0 是便携源码工具预览版，提供显式验钥与初始化、认证数据库/WAL 快照、增量归档、按会话和日期整理上下文、原始行保留及分页聊天浏览。

只处理本人拥有或已获授权的数据。归档、密钥和聊天正文都留在本机。

## 多种封装，共用一个核心

当前提供命令行、本地网页、只读 API 和当前用户的登录启动项；两个平台包共用同一个核心。桌面 App 和 MCP 可在这套核心上接入。详见[本地 API 与封装关系](docs/local-api.md)。

## 平台交付

| 交付 | 内容与要求 |
| --- | --- |
| macOS 便携包 | 公共 wheel、`launch.command`、说明与许可；需要 Python 3.11+ |
| Windows 便携包 | 同一个 wheel、`start.cmd`、说明与许可；建议 x64 Python 3.11+ 并安装 Python Launcher |
| 源码包 | 所有公共模块、合成测试、CI 与打包脚本 |

这些包不包含 Python、私人数据、腾讯微信二进制或本机配置。首次安装依赖需要联网，之后聊天读取和页面浏览都在本地；不是原生 App/EXE 安装包。

解压后启动对应脚本，根据提示输入已有 `archive.sqlite` 路径即可打开归档。首次获取或导入密钥、生成归档请执行下面的显式命令。运行版按当前会话分页查询，每页 100 条，不把全部历史嵌入首页。

## 从源码安装

```sh
python -m venv .venv
```

后续所有 `python` 替换为虚拟环境中的解释器：macOS 为 `.venv/bin/python`；Windows 为 `.venv\Scripts\python.exe`。也可先激活虚拟环境。

```sh
python -m pip install .
python -m wechat_archive --help
```

## 使用已有密钥

密钥 JSON 支持每库 `keys` / `enc_key` 或按盐值的 `keys_by_salt` 格式，必须对当前账号逐库认证。Mac 上输入密钥文件权限应为 600；示例中的路径需换成本机真实路径。

```sh
python -m wechat_archive archive --source "/path/to/db_storage" --keys-json "/private/verified-keys.json" --output "/private/archive"
python -m wechat_archive serve --archive "/private/archive/archive.sqlite"
```

`source` 选择现有账号的 `db_storage` 目录。输出必须位于源目录以外；换来源时使用新输出目录，避免覆盖其他账号。浏览命令会显示含本次会话认证令牌的 `127.0.0.1` 地址，请在浏览器打开完整地址，不分享该地址；Ctrl+C 停止。

也可以先将验证后的密钥存入系统凭据存储：

```sh
python -m wechat_archive import-keys --source "/path/to/db_storage" --input "/private/verified-keys.json" --state "/private/credentials"
python -m wechat_archive archive --source "/path/to/db_storage" --key-store "/private/credentials/keys.protected" --output "/private/archive"
```

Mac 使用钥匙串，Windows 使用当前用户 DPAPI。保护存储不能跨设备直接使用；程序不回显密钥。

## 持续更新与本地上下文

已有受保护密钥后，使用 JSON 配置连接现有源、归档输出和密钥文件。以下命令均应使用安装本项目的同一个虚拟环境解释器；路径仅为示例：

```sh
python -m wechat_archive watch --config "/private/example/runner.json" --once
python -m wechat_archive watch --config "/private/example/runner.json"
python -m wechat_archive process-context --archive "/private/example/archive-output/archive.sqlite" --output "/private/example/context-output" --batch-size 10000
```

`--once` 只运行一轮；省略后持续轮询。电脑开机、用户已登录、系统保持唤醒且源微信能够接收新消息时，持续归档才会更新。第一版上下文处理按会话和日期建立可追溯的本地索引，不生成语义摘要，也不涵盖未导入的外部来源。

若需随当前用户登录自动运行：

```sh
python -m wechat_archive service-install --config "/private/example/runner.json"
python -m wechat_archive service-uninstall
```

macOS 使用当前用户 LaunchAgent，Windows 使用当前用户计划任务，无需管理员权限。安装项沿用当前虚拟环境的 Python 和依赖；卸载保留归档、密钥及配置。持续运行不会重新取钥、重新签名或重启微信。配置字段、状态文件和故障重试见[本地持续归档说明](docs/continuous.md)。

## 首次取钥：各端独立

Windows 采用已有适配的官方进程内存扫描器，微信需已登录运行。当前权限不足时可能需要管理员权限。

```sh
python -m wechat_archive bootstrap --source "/path/to/db_storage" --state "/private/credentials"
```

macOS 初始化采用临时副本 Frida 路线，需要显式允许微信重启。先安装可选依赖；官方 `/Applications/WeChat.app` 必须已经运行。

```sh
python -m pip install '.[mac-key]'
python -m wechat_archive bootstrap --source "/path/to/db_storage" --state "/private/credentials" --allow-wechat-restart
```

便携包使用者可在其虚拟环境单独安装 `frida==17.23.3`，然后运行上述初始化命令。它正常退出官方微信，仅对临时副本签名，捕获并验证候选后停止副本及其所属进程，再恢复官方微信。不会覆盖或改签官方应用，也不关闭 SIP。

**集成后的取钥恢复封装仍为实验性功能**：底层捕获和读取核心有此前真机证据；新的统一封装使用合成故障测试，本版没有重复执行真机取钥。系统权限、钥匙串锁定、微信升级或扫码登录都可能使初始化失败，不能把捕获脚本退出当作成功。正常浏览使用现有密钥，不反复取钥。

## 已有认证快照

```sh
python -m wechat_archive parse --snapshots "/private/snapshot-set" --output "/private/parsed-archive"
```

输入目录含 `snapshot-status.json` 与 `databases/`。输出绑定来源；不同来源会拒绝替换既有归档。该导入入口读取快照台账，并非重新证明外部快照的密文认证过程；只导入自己可信的认证快照。

## 范围与限制

- 默认纳入群聊、私聊、服务号、系统及未映射消息表，保留原始行；不是云端或其他设备的完整历史。
- DB/WAL 是逐库一致快照，不是全部数据库同时冻结。
- 三类微信专用全文索引可能需要 `MMFtsTokenizer`，无法完成一般 SQLite 完整性检查时明确标注，不冒记为通过。
- 当前文字、压缩正文、分享卡片和系统事件可解析；媒体记录保留，媒体本体未解码。
- 本版不包含跨设备合并、媒体播放、语义摘要或原生桌面安装包。持续采集依赖本机登录、唤醒和现有密钥；登录启动项不是系统级服务。
- 分页界面已经做合成状态/异步/分页测试；尚无真实浏览器界面验收。CI 的 Windows/Mac 测试不等于任意微信版本真机取钥通过。

## 测试与打包

```sh
python -m unittest discover -s tests -q
node --test tests/ui.test.cjs
python -m pip install build 'setuptools>=77,<83'
python scripts/package.py --output dist --platform all
```

测试只使用临时合成库、模拟钥匙串和公开 AES 向量，不读取私人微信数据。详见[架构](docs/architecture.md)、[交付范围](docs/release-scope.md)和[来源与许可](THIRD_PARTY.md)。
