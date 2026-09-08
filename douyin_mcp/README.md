# Douyin Downloader MCP

Local stdio MCP server extracted from `TikTokDownloader`'s Douyin parsing flow.

Tools:

- `parse_douyin_video`: resolve a Douyin short/full link and return metadata plus download URLs.
- `download_douyin_video`: resolve and download the work locally without requiring cookies.

Run a quick test:

```powershell
.\.venv\Scripts\python.exe .\douyin_mcp\server.py --test-url "https://v.douyin.com/CvX4_gydGcM/"
```

Start as MCP:

```powershell
.\.venv\Scripts\python.exe .\douyin_mcp\server.py
```

When `output_dir` is omitted, downloads are saved under `downloads\douyin`.
