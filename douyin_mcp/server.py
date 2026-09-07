from __future__ import annotations

import argparse
import asyncio
import importlib.util
import json
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from time import localtime, strftime
from typing import Any
from urllib.parse import quote, urlencode, urlparse

import httpx
from mcp.server.fastmcp import FastMCP


PROJECT_ROOT = Path(__file__).resolve().parents[1]
TIKTOK_DOWNLOADER_ROOT = PROJECT_ROOT / "TikTokDownloader"
TIKTOK_DOWNLOADER_SRC = TIKTOK_DOWNLOADER_ROOT / "src"
if str(TIKTOK_DOWNLOADER_ROOT) not in sys.path:
    sys.path.insert(0, str(TIKTOK_DOWNLOADER_ROOT))

from src.custom import DATA_HEADERS, DOWNLOAD_HEADERS, USERAGENT  # noqa: E402


def load_abogus_class():
    path = TIKTOK_DOWNLOADER_SRC / "encrypt" / "aBogus.py"
    spec = importlib.util.spec_from_file_location("td_abogus", path)
    if not spec or not spec.loader:
        raise RuntimeError(f"无法加载 ABogus: {path}")
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module.ABogus


ABogus = load_abogus_class()


DEFAULT_OUTPUT_DIR = PROJECT_ROOT / "downloads" / "douyin"
DOUYIN_DETAIL_API = "https://www.douyin.com/aweme/v1/web/aweme/detail/"
DOUYIN_REFERER = "https://www.douyin.com/?recommend=1"

DETAIL_PATTERNS = (
    re.compile(r"https://www\.douyin\.com/(?:video|note|slides)/([0-9]{19})"),
    re.compile(r"https://www\.iesdouyin\.com/share/(?:video|note|slides)/([0-9]{19})/"),
    re.compile(r"\bmodal_id=([0-9]{19})\b"),
    re.compile(r"\b([0-9]{19})\b"),
)

ILLEGAL_FILENAME_CHARS = re.compile(r'[\\/:*?"<>|\r\n\t]+')


class DouyinDownloadError(RuntimeError):
    pass


@dataclass(slots=True)
class ParsedWork:
    aweme_id: str
    item_type: str
    title: str
    author: str
    create_time: str
    downloads: list[str]
    raw: dict[str, Any]


def safe_filename(value: str, fallback: str, max_length: int = 120) -> str:
    value = ILLEGAL_FILENAME_CHARS.sub("_", value).strip(" ._")
    value = re.sub(r"\s+", " ", value)
    if not value:
        value = fallback
    return value[:max_length].strip(" ._") or fallback


def first_url(data: Any) -> str:
    if isinstance(data, dict):
        urls = data.get("url_list")
        if isinstance(urls, list) and urls:
            return str(urls[-1] or urls[0])
        uri = data.get("uri")
        if isinstance(uri, str):
            return uri
    return ""


def normalize_video_url(url: str) -> str:
    if not url:
        return ""
    return url.replace("playwm", "play")


def build_detail_params(aweme_id: str) -> dict[str, str]:
    return {
        "device_platform": "webapp",
        "aid": "6383",
        "channel": "channel_pc_web",
        "update_version_code": "170400",
        "pc_client_type": "1",
        "pc_libra_divert": "Windows",
        "support_h265": "1",
        "support_dash": "1",
        "version_code": "190500",
        "version_name": "19.5.0",
        "cookie_enabled": "true",
        "screen_width": "1536",
        "screen_height": "864",
        "browser_language": "zh-CN",
        "browser_platform": "Win32",
        "browser_name": "Chrome",
        "browser_version": "139.0.0.0",
        "browser_online": "true",
        "engine_name": "Blink",
        "engine_version": "139.0.0.0",
        "os_name": "Windows",
        "os_version": "10",
        "cpu_core_num": "16",
        "device_memory": "8",
        "platform": "PC",
        "downlink": "10",
        "effective_type": "4g",
        "round_trip_time": "200",
        "uifid": "",
        "msToken": "",
        "aweme_id": aweme_id,
    }


def sign_params(params: dict[str, str]) -> str:
    encoded = urlencode(params, safe="=", quote_via=quote)
    return f"{encoded}&a_bogus={ABogus(USERAGENT, 'Win32').get_value(encoded, 'GET')}"


def extract_detail_id(text: str) -> str | None:
    for pattern in DETAIL_PATTERNS:
        if match := pattern.search(text):
            return match.group(1)
    return None


def parse_aweme_item(item: dict[str, Any]) -> ParsedWork:
    aweme_id = str(item.get("aweme_id") or item.get("id") or "")
    desc = str(item.get("desc") or "")
    author = item.get("author") or {}
    author_name = str(author.get("nickname") or author.get("unique_id") or "")
    create_time = ""
    if ts := item.get("create_time"):
        create_time = strftime("%Y-%m-%d %H.%M.%S", localtime(int(ts)))

    images = item.get("images")
    if isinstance(images, list) and images:
        downloads = []
        for image in images:
            if isinstance(image, dict):
                downloads.append(first_url(image.get("url_list")))
            elif isinstance(image, list):
                downloads.append(first_url({"url_list": image}))
        downloads = [u for u in downloads if u]
        item_type = "image"
    else:
        video = item.get("video") or {}
        bit_rate = video.get("bit_rate")
        url = ""
        if isinstance(bit_rate, list) and bit_rate:
            candidates = sorted(
                bit_rate,
                key=lambda entry: int(entry.get("bit_rate") or 0)
                if isinstance(entry, dict)
                else 0,
                reverse=True,
            )
            for entry in candidates:
                if isinstance(entry, dict):
                    url = first_url(entry.get("play_addr"))
                    if url:
                        break
        url = url or first_url(video.get("play_addr"))
        url = normalize_video_url(url)
        downloads = [url] if url else []
        item_type = "video"

    title_parts = [create_time, author_name, desc or aweme_id]
    title = safe_filename("-".join(i for i in title_parts if i), aweme_id)
    if not downloads:
        raise DouyinDownloadError("没有从作品数据中提取到可下载地址")

    return ParsedWork(
        aweme_id=aweme_id,
        item_type=item_type,
        title=title,
        author=author_name,
        create_time=create_time,
        downloads=downloads,
        raw=item,
    )


def parse_aweme_detail(detail: dict[str, Any]) -> ParsedWork:
    return parse_aweme_item(detail)


class DouyinClient:
    def __init__(self, timeout: float = 20.0, proxy: str | None = None):
        self.timeout = timeout
        self.proxy = proxy

    async def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(
            follow_redirects=True,
            verify=False,
            timeout=self.timeout,
            proxy=self.proxy,
            headers={"User-Agent": USERAGENT},
        )

    async def resolve_url(self, text: str) -> str:
        async with await self._client() as client:
            response = await client.get(text, headers=DATA_HEADERS)
            response.raise_for_status()
            return str(response.url)

    async def extract_id(self, text: str) -> tuple[str, str]:
        direct = extract_detail_id(text)
        if direct:
            return direct, text

        resolved = await self.resolve_url(text)
        resolved_id = extract_detail_id(resolved)
        if resolved_id:
            return resolved_id, resolved
        raise DouyinDownloadError(f"无法从链接中解析抖音作品 ID: {text}")

    async def fetch_detail(self, aweme_id: str) -> dict[str, Any]:
        params = sign_params(build_detail_params(aweme_id))
        headers = DATA_HEADERS.copy()
        headers["Referer"] = f"https://www.douyin.com/video/{aweme_id}"
        async with await self._client() as client:
            response = await client.get(f"{DOUYIN_DETAIL_API}?{params}", headers=headers)
            response.raise_for_status()
            try:
                data = response.json()
            except ValueError:
                data = None
            if isinstance(data, dict):
                detail = data.get("aweme_detail")
                if isinstance(detail, dict) and detail:
                    return detail

            page = await client.get(
                f"https://www.iesdouyin.com/share/video/{aweme_id}/",
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_4 like Mac OS X) "
                        "AppleWebKit/605.1.15 (KHTML, like Gecko) "
                        "CriOS/125.0.6422.51 Mobile/15E148 Safari/604.1"
                    )
                },
            )
            page.raise_for_status()
            html = page.text

        if "status_audit_not_pass" in html:
            raise DouyinDownloadError("该作品处于不可公开访问状态，无法无 cookie 下载")
        if match := re.search(r'"videoInfoRes":(\{.*?\}),"itemId"', html):
            info = json.loads(match.group(1))
            item_list = info.get("item_list")
            if isinstance(item_list, list) and item_list:
                return item_list[0]
            raise DouyinDownloadError(
                f"作品未返回可下载内容: {info.get('filter_list') or info}"
            )
        raise DouyinDownloadError("未能从作品详情页解析到作品数据")

    async def parse(self, url_or_text: str) -> ParsedWork:
        aweme_id, _ = await self.extract_id(url_or_text)
        detail = await self.fetch_detail(aweme_id)
        return parse_aweme_item(detail)

    async def download_file(self, url: str, path: Path) -> int:
        path.parent.mkdir(parents=True, exist_ok=True)
        headers = DOWNLOAD_HEADERS.copy()
        headers["Referer"] = DOUYIN_REFERER
        total = 0
        async with await self._client() as client:
            async with client.stream("GET", url, headers=headers) as response:
                response.raise_for_status()
                with path.open("wb") as file:
                    async for chunk in response.aiter_bytes(1024 * 256):
                        if chunk:
                            file.write(chunk)
                            total += len(chunk)
        if total == 0:
            raise DouyinDownloadError(f"下载结果为空: {url}")
        return total

    async def download(self, url_or_text: str, output_dir: str | None = None) -> dict[str, Any]:
        work = await self.parse(url_or_text)
        root = Path(output_dir).expanduser() if output_dir else DEFAULT_OUTPUT_DIR
        root.mkdir(parents=True, exist_ok=True)

        files: list[dict[str, Any]] = []
        if work.item_type == "video":
            path = root / f"{work.title}.mp4"
            size = await self.download_file(work.downloads[0], path)
            files.append({"path": str(path.resolve()), "bytes": size})
        else:
            folder = root / work.title
            for index, url in enumerate(work.downloads, start=1):
                path = folder / f"{work.aweme_id}_{index}.jpeg"
                size = await self.download_file(url, path)
                files.append({"path": str(path.resolve()), "bytes": size})

        return {
            "aweme_id": work.aweme_id,
            "type": work.item_type,
            "title": work.title,
            "author": work.author,
            "create_time": work.create_time,
            "download_urls": work.downloads,
            "files": files,
        }


mcp = FastMCP("douyin-downloader")


@mcp.tool()
async def parse_douyin_video(url: str) -> dict[str, Any]:
    """解析抖音作品链接，返回作品 ID、标题、作者和无 cookie 可用下载地址。"""
    work = await DouyinClient().parse(url)
    return {
        "aweme_id": work.aweme_id,
        "type": work.item_type,
        "title": work.title,
        "author": work.author,
        "create_time": work.create_time,
        "download_urls": work.downloads,
    }


@mcp.tool()
async def download_douyin_video(url: str, output_dir: str | None = None) -> dict[str, Any]:
    """解析并下载抖音作品到本地目录；不需要传入 cookie。"""
    return await DouyinClient().download(url, output_dir)


async def run_test(url: str, output_dir: str | None) -> None:
    result = await DouyinClient().download(url, output_dir)
    print(json.dumps(result, ensure_ascii=False, indent=2))


def main() -> None:
    parser = argparse.ArgumentParser(description="Local MCP server for Douyin downloads.")
    parser.add_argument("--test-url", help="Run one download test instead of starting MCP.")
    parser.add_argument("--output-dir", help="Output directory for --test-url.")
    args = parser.parse_args()
    if args.test_url:
        asyncio.run(run_test(args.test_url, args.output_dir))
    else:
        mcp.run()


if __name__ == "__main__":
    main()
