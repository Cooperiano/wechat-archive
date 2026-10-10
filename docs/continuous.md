# 本地持续归档

本说明面向在**已完成密钥初始化**后运行本地增量归档的用户。运行器只读取现有受保护密钥和源数据库；它不会重新取密钥、重新签名、启动或重启微信。手动运行或安装当前用户的登录启动项后，只要电脑开机、用户已登录、系统保持唤醒且源微信能够接收新消息，程序就按配置轮询。休眠或关机期间不采集，恢复后再从已有归档继续。

先在本机私有位置创建 JSON 配置，填写本机的绝对路径。以下路径只是示例，不能直接作为真实账号路径使用：

```json
{
  "source": "/private/example/wechat-source",
  "output": "/private/example/archive-output",
  "key_store": "/private/example/state/keys.protected",
  "context_output": "/private/example/context-output",
  "interval_seconds": 120,
  "context_batch_size": 10000
}
```

`source` 是现有数据库目录，`key_store` 是此前初始化生成的受保护文件，`output` 是归档目录。`context_output` 可省略；设置后每轮增量采集成功时按批处理上下文记录，积压会在后续轮次继续。`context_batch_size` 默认 10000，允许 1–10000 条。上下文目录可以设在 `output` 里面，但不能占用归档程序保留的文件或目录。配置文件应放在源、上下文和归档保留路径以外，避免被归档写入覆盖。`interval_seconds` 默认 120，允许 30–86400 秒；有上下文积压时以 5 秒间隔继续处理。所有路径须为绝对路径，源和密钥文件不得与输出目录重叠。运行前会验证配置和路径边界；缺失的源或密钥会进入等待重试状态。

在已经安装本项目及依赖的 Python 虚拟环境中，使用**同一个解释器**从终端启动持续运行：

```sh
python -m wechat_archive watch --config "/private/example/runner.json"
```

需要只执行一轮以检查设置时：

```sh
python -m wechat_archive watch --config "/private/example/runner.json" --once
```

已有归档也可以单独整理上下文：

```sh
python -m wechat_archive process-context --archive "/private/example/archive-output/archive.sqlite" --output "/private/example/context-output" --batch-size 10000
```

第一版上下文处理按会话和日期建立可追溯的本地索引，用于查看已有消息及其来源；它不生成语义摘要，也不处理未导入的外部数据源。`process-context` 只处理现有归档，不启动持续采集。

如需在当前用户每次登录后自动运行，可在**同一个已安装依赖的虚拟环境**中执行：

```sh
python -m wechat_archive service-install --config "/private/example/runner.json"
python -m wechat_archive service-uninstall
```

`service-install` 在 macOS 创建当前用户的 LaunchAgent，在 Windows 创建当前用户登录触发的计划任务；两者都无需管理员权限，并保留当前虚拟环境的 Python 路径。安装后会启动持续运行；卸载会停止并移除该登录启动项，保留归档、上下文、密钥和配置文件。登录启动项依赖当前虚拟环境及其软件包继续存在，移动或删除该环境后需重新安装启动项。此命令不会执行 `bootstrap` 或自动重新签名。

手动运行时用 `Ctrl+C` 结束进程。调用方也可传入 `threading.Event` 作为 `stop_event`，设置事件即可在当前轮结束后或等待期间退出。运行器用 `output/.watch.lock` 防止同一个输出目录同时启动第二个持续归档进程。它将 `output/watch-status.json` 写为仅含固定状态、时间、计数和固定原因代码的本地状态文件；不包含源路径、消息 ID、正文、密钥或异常详情。`complete` 表示本轮采集与启用的上下文处理均完成；`partial/context_backlog` 表示上下文还有待处理记录；`waiting` 或 `retrying` 表示会在后续轮次重试。状态文件的最近成功时间表示上一次完整轮次，而不是当前轮次的成功证明。

归档数据和密钥都应留在当前设备的私有目录。持续运行和登录启动均不上传或发布数据。
