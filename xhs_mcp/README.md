# 小红书内容与配图 MCP

一个本地 stdio MCP 服务，包含两个工具：

- `collect_xhs_benchmark_notes`：输入搜索关键词，通过 TikHub 小红书接口搜索图文笔记，按默认综合数据取前 3 篇，提取标题、正文、话题并保存 Markdown 到项目根目录。
- `generate_xhs_style_images`：读取上一步保存的 Markdown，按小红书提示词框架改写为 3 个提示词，使用 `Doubao-Seedream-5.0-lite` 生成 3 张 PNG 配图并保存到项目根目录。

密钥读取：

```text
C:\Users\admin\Desktop\小红书能力包搭建\mcp\.env
```

需要包含：

```text
TIKHUB_KEY=...
DOUBAO_API_KEY=...
```

运行 MCP：

```powershell
.\.venv\Scripts\python.exe .\xhs_mcp\server.py
```

本地测试关键词收集：

```powershell
.\.venv\Scripts\python.exe .\xhs_mcp\server.py --test-keyword "美食推荐"
```

本地测试配图：

```powershell
.\.venv\Scripts\python.exe .\xhs_mcp\server.py --test-images
```

默认输出：

- Markdown：`xhs_benchmark_<关键词>_<时间>.md`
- 最新 Markdown 副本：`xhs_benchmark_latest.md`
- 图片：`xhs_seedream_<时间>_1.png` 到 `xhs_seedream_<时间>_3.png`
