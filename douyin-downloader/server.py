from __future__ import annotations

import argparse
import asyncio
import json
import sys
from typing import Any

from mcp.server.fastmcp import FastMCP

from douyin_downloader import DouyinClient

mcp = FastMCP("douyin-downloader")


@mcp.tool()
async def parse_douyin_video(url: str) -> dict[str, Any]:
    """Parse a public Douyin video URL without user cookies."""
    video = await DouyinClient().parse(url)
    return {
        "aweme_id": video.aweme_id,
        "type": "video",
        "title": video.title,
        "author": video.author,
        "create_time": video.create_time,
        "download_url": video.download_url,
        "audio_url": video.audio_url,
        "page_url": video.page_url,
    }


@mcp.tool()
async def download_douyin_video(url: str, output_dir: str | None = None) -> dict[str, Any]:
    """Download and save a public Douyin video without user cookies."""
    return await DouyinClient().download(url, output_dir)


@mcp.tool()
async def download_douyin_videos(urls: list[str], output_dir: str | None = None) -> dict[str, Any]:
    """Download multiple public Douyin video URLs to local disk without requiring cookies."""
    return await DouyinClient().download_many(urls, output_dir)


async def run_test(url: str, output_dir: str | None) -> None:
    result = await DouyinClient().download(url, output_dir)
    print_json(result)


async def run_tests(urls: list[str], output_dir: str | None) -> None:
    result = await DouyinClient().download_many(urls, output_dir)
    print_json(result)


def print_json(data: dict[str, Any]) -> None:
    text = json.dumps(data, ensure_ascii=False, indent=2)
    try:
        print(text)
    except UnicodeEncodeError:
        sys.stdout.buffer.write(text.encode("utf-8", errors="replace") + b"\n")


def main() -> None:
    parser = argparse.ArgumentParser(description="Standalone local MCP server for Douyin video downloads.")
    parser.add_argument("--test-url", help="Run one download test instead of starting MCP.")
    parser.add_argument("--test-urls", nargs="*", help="Run multiple download tests instead of starting MCP.")
    parser.add_argument("--output-dir", help="Output directory for --test-url.")
    args = parser.parse_args()
    if args.test_urls:
        asyncio.run(run_tests(args.test_urls, args.output_dir))
    elif args.test_url:
        asyncio.run(run_test(args.test_url, args.output_dir))
    else:
        mcp.run()


if __name__ == "__main__":
    main()
