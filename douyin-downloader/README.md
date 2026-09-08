# Douyin Downloader MCP

一个独立的本地 MCP 小项目，只保留“抖音公开视频解析与下载”这一件事。

它不依赖原项目的账号、评论、列表、数据库等能力，也不要求你手动提供 cookie。它通过本地浏览器打开公开视频页，捕获可下载的音视频流，再在本地合并为 MP4。

## 功能

 - 解析单个抖音公开视频链接
 - 下载单个公开视频并保存到本地
 - 批量下载多个公开视频链接
 - 作为本地 MCP server 通过 stdio 提供给 Codex 使用

## 目录结构

```text
douyin-downloader/
  server.py
  requirements.txt
  README.md
  douyin_downloader/
    downloader.py
  downloads/
  .venv/
```

## 环境要求

 - Windows
 - Python 3.12
 - 本地 Chrome 或 Edge
 - 能正常访问 Douyin 公共页面

## 安装

在项目根目录执行：

```powershell
cd "C:\Users\admin\Documents\New project\douyin-downloader"
py -3.12 -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe -m playwright install chromium
```

如果你的机器上已经有 Chrome/Edge，也可以直接使用本地浏览器。默认会自动寻找；找不到时，可手动设置：

```powershell
setx DOUYIN_BROWSER_PATH "C:\Program Files\Google\Chrome\Application\chrome.exe"
```

## 启动 MCP

```powershell
.\.venv\Scripts\python.exe .\server.py
```

MCP 会以 stdio 方式启动，适合被 Codex 直接挂载使用。

## 可用工具

 - `parse_douyin_video`：解析单个公开视频，返回作品信息和媒体地址
 - `download_douyin_video`：下载单个公开视频到本地
 - `download_douyin_videos`：批量下载多个公开视频到本地

## MCP 工具接口

### `parse_douyin_video`

参数：

```json
{
  "url": "抖音公开视频链接"
}
```

返回：

```json
{
  "aweme_id": "作品 ID",
  "type": "video",
  "title": "作品标题",
  "author": "作者昵称",
  "create_time": "",
  "download_url": "捕获到的视频流地址",
  "audio_url": "捕获到的音频流地址",
  "page_url": "跳转后的公开视频页"
}
```

### `download_douyin_video`

参数：

```json
{
  "url": "抖音公开视频链接",
  "output_dir": "可选，本地保存目录"
}
```

返回：

```json
{
  "aweme_id": "作品 ID",
  "type": "video",
  "title": "作品标题",
  "author": "作者昵称",
  "create_time": "",
  "download_url": "捕获到的视频流地址",
  "audio_url": "捕获到的音频流地址",
  "page_url": "跳转后的公开视频页",
  "file": {
    "path": "保存后的 MP4 绝对路径",
    "bytes": 83126961
  }
}
```

### `download_douyin_videos`

参数：

```json
{
  "urls": [
    "抖音公开视频链接 1",
    "抖音公开视频链接 2"
  ],
  "output_dir": "可选，本地保存目录"
}
```

返回：

```json
{
  "output_dir": "实际保存目录",
  "total": 2,
  "success": 2,
  "failed": 0,
  "items": [
    {
      "index": 1,
      "url": "原始链接",
      "ok": true,
      "file": {
        "path": "保存后的 MP4 绝对路径",
        "bytes": 83126961
      }
    }
  ]
}
```

批量下载会逐条处理链接；某一条失败时不会中断整个批次，失败项会返回 `ok: false` 和 `error`。

## 单个链接示例

传入一个公开视频链接：

```json
{
  "url": "https://v.douyin.com/3fqL5tE37Ts/"
}
```

返回结果里会包含：

 - `aweme_id`
 - `title`
 - `author`
 - `download_url`
 - `audio_url`
 - `page_url`
 - `file.path`
 - `file.bytes`

## 批量下载示例

批量工具 `download_douyin_videos` 接收一个链接数组：

```json
{
  "urls": [
    "https://v.douyin.com/3fqL5tE37Ts/",
    "https://v.douyin.com/ddspUaZ0Sp8/"
  ],
  "output_dir": "C:\\Users\\admin\\Documents\\New project\\douyin-downloader\\downloads"
}
```

返回结果会包含：

 - `output_dir`：保存目录
 - `total`：总链接数
 - `success`：成功数
 - `failed`：失败数
 - `items`：逐条结果，包含 `ok`、`file` 或 `error`

## 命令行测试

单个链接测试：

```powershell
.\.venv\Scripts\python.exe .\server.py --test-url "https://v.douyin.com/3fqL5tE37Ts/"
```

批量链接测试：

```powershell
.\.venv\Scripts\python.exe .\server.py --test-urls "https://v.douyin.com/3fqL5tE37Ts/" "https://v.douyin.com/ddspUaZ0Sp8/"
```

如果不传 `output_dir`，默认保存到项目下的 `downloads` 目录。

## Codex 本地 MCP 配置

把下面内容放到 `C:\Users\admin\zktst\.codex\config.toml`：

```toml
[mcp_servers.douyin_downloader]
command = 'C:\Users\admin\Documents\New project\douyin-downloader\.venv\Scripts\python.exe'
args = ['C:\Users\admin\Documents\New project\douyin-downloader\server.py']
cwd = 'C:\Users\admin\Documents\New project\douyin-downloader'
startup_timeout_sec = 120

[mcp_servers.douyin_downloader.env]
DOUYIN_BROWSER_PATH = 'C:\Program Files\Google\Chrome\Application\chrome.exe'
```

## 常见问题

 - 如果提示无法启动浏览器，先确认 Chrome/Edge 已安装，或者设置 `DOUYIN_BROWSER_PATH`。
 - 如果 Playwright 报浏览器缺失，执行 `python -m playwright install chromium`。
 - 目前只支持公开视频，不支持需要登录、cookie、私密或已删除的视频。
 - 批量下载时，单条失败不会中断整个任务，结果会写进 `items`。
 - 文件名会自动清洗，适合 Windows 保存；同名文件会自动追加序号，避免覆盖。

## 备注

这个项目的定位是“一个独立、可挂载、只做抖音视频下载”的本地 MCP 工具。
