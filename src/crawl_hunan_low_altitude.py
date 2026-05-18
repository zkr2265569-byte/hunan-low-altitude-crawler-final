# -*- coding: utf-8 -*-
"""
湖南省工业和信息化厅站内搜索“低空经济”结果抓取器。

功能：
1. 打开搜索页，自动翻页/滚动/监听网络响应，收集搜索结果链接；
2. 逐个进入详情页，抽取：标题、正文、信息来源、发布时间、原文链接；
3. 对正文与标题进行“低空经济”相关性复核，过滤搜索拆词噪声；
4. 输出：TXT、CSV、JSONL、原始链接表与日志。

注意：
- 请遵守目标网站 robots.txt、使用条款和访问频率要求。
- 默认延时较保守；如需大规模全量抓取，不要把 delay 调得过低。
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import random
import re
import sys
import time
from dataclasses import asdict, dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Iterable, Optional
from urllib.parse import unquote, urljoin, urlparse

import pandas as pd
import requests
from bs4 import BeautifulSoup
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

SEARCH_URL = "https://searching.hunan.gov.cn/hunan/105000000/news?q=%E4%BD%8E%E7%A9%BA%E7%BB%8F%E6%B5%8E"
KEYWORD = "低空经济"
ALLOWED_DOMAINS = (
    "hunan.gov.cn",
    "gxt.hunan.gov.cn",
    "searching.hunan.gov.cn",
)

BAD_LINK_PATTERNS = (
    "javascript:",
    "mailto:",
    "#",
    "beian.miit.gov.cn",
    "www.beian.gov.cn",
    "zwfw-new.hunan.gov.cn",
)

ARTICLE_URL_HINTS = (
    "/t20",       # 湖南政府站群常见详情页路径
    ".html",
    "/content_",
    "/xxgk",
    "/gzdt",
    "/zcfg",
)

STOP_TEXT_MARKERS = [
    "打印本页",
    "关闭本页",
    "收藏",
    "国家部委网站",
    "各省工信厅网站",
    "各市州工信局网站",
    "主办单位：",
    "政府网站标识码",
    "备案号：",
    "湘公网安备",
]

NAV_NOISE_PATTERNS = [
    r"^首页$",
    r"^网站首页$",
    r"^政府信息公开$",
    r"^办事服务$",
    r"^互动交流$",
    r"^专题专栏$",
    r"^无障碍浏览$",
    r"^站内搜索$",
    r"^大 中 小$",
    r"^字体：.*$",
]


@dataclass
class LinkItem:
    url: str
    anchor_text: str = ""
    found_from: str = "dom"


@dataclass
class Article:
    index: int
    title: str
    publish_time: str
    source: str
    url: str
    body: str
    body_chars: int
    collected_at: str


def setup_logging(output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)
    log_path = output_dir / "crawl.log"
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        handlers=[
            logging.FileHandler(log_path, encoding="utf-8"),
            logging.StreamHandler(sys.stdout),
        ],
    )


def normalize_space(text: str) -> str:
    text = text.replace("\xa0", " ")
    text = re.sub(r"[\t\r\f\v]+", " ", text)
    text = re.sub(r" *\n *", "\n", text)
    text = re.sub(r"[ ]{2,}", " ", text)
    text = re.sub(r"\n{3,}", "\n\n", text)
    return text.strip()


def compact_cn(text: str) -> str:
    return re.sub(r"\s+", "", text or "")


def clean_anchor_text(text: str) -> str:
    text = normalize_space(text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()


def is_allowed_domain(url: str) -> bool:
    try:
        netloc = urlparse(url).netloc.lower()
    except Exception:
        return False
    return any(netloc == d or netloc.endswith("." + d) for d in ALLOWED_DOMAINS)


def is_bad_link(url: str) -> bool:
    low = url.lower().strip()
    if not low.startswith(("http://", "https://")):
        return True
    return any(p in low for p in BAD_LINK_PATTERNS)


def looks_like_article_url(url: str) -> bool:
    low = url.lower()
    if not is_allowed_domain(url) or is_bad_link(url):
        return False
    # 排除图片、视频、压缩包、脚本、样式等资源。
    if re.search(r"\.(jpg|jpeg|png|gif|webp|svg|mp4|avi|mov|zip|rar|7z|css|js)(\?|$)", low):
        return False
    return any(hint in low for hint in ARTICLE_URL_HINTS)


def dedupe_links(items: Iterable[LinkItem]) -> list[LinkItem]:
    seen: set[str] = set()
    out: list[LinkItem] = []
    for item in items:
        # 去掉 URL fragment，保留 query，避免同一文章重复。
        parsed = urlparse(item.url)
        normalized = parsed._replace(fragment="").geturl()
        if normalized in seen:
            continue
        seen.add(normalized)
        out.append(LinkItem(url=normalized, anchor_text=item.anchor_text, found_from=item.found_from))
    return out


def extract_urls_from_json(obj: Any, base_url: str) -> list[LinkItem]:
    """从搜索接口的 JSON 响应中递归提取 URL 与相邻标题字段。"""
    found: list[LinkItem] = []

    def walk(x: Any, title_hint: str = "") -> None:
        if isinstance(x, dict):
            local_title = title_hint
            for tk in ("title", "name", "docTitle", "displayTitle", "contentTitle"):
                if isinstance(x.get(tk), str) and x.get(tk).strip():
                    local_title = clean_anchor_text(re.sub(r"<[^>]+>", "", x[tk]))
                    break
            for uk in ("url", "href", "link", "docUrl", "contentUrl", "pageUrl", "siteUrl"):
                val = x.get(uk)
                if isinstance(val, str) and val.strip():
                    u = urljoin(base_url, val.strip())
                    if looks_like_article_url(u):
                        found.append(LinkItem(url=u, anchor_text=local_title, found_from="network-json"))
            for v in x.values():
                walk(v, local_title)
        elif isinstance(x, list):
            for v in x:
                walk(v, title_hint)
        elif isinstance(x, str):
            # 有些接口会把 URL 藏在字符串字段里。
            for m in re.finditer(r"https?://[^\s'\"<>]+", x):
                u = m.group(0)
                if looks_like_article_url(u):
                    found.append(LinkItem(url=u, anchor_text=title_hint, found_from="network-json-string"))

    walk(obj)
    return found


def collect_search_links(
    search_url: str,
    max_pages: int,
    max_results: int,
    headless: bool,
    delay: float,
) -> list[LinkItem]:
    """使用 Playwright 收集搜索结果链接。兼容分页、滚动加载和 JSON 接口。"""
    collected: list[LinkItem] = []
    network_collected: list[LinkItem] = []

    def maybe_add_network_response(response) -> None:  # noqa: ANN001
        try:
            url = response.url
            ctype = (response.headers.get("content-type") or "").lower()
            if "json" not in ctype and not any(k in url.lower() for k in ("search", "searching", "query")):
                return
            body_text = response.text()
            if not body_text or not any(k in body_text for k in ("低空", "经济", "url", "href")):
                return
            try:
                data = json.loads(body_text)
            except Exception:
                return
            items = extract_urls_from_json(data, search_url)
            if items:
                network_collected.extend(items)
        except Exception:
            return

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=headless)
        context = browser.new_context(
            viewport={"width": 1440, "height": 1200},
            user_agent=(
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            locale="zh-CN",
        )
        page = context.new_page()
        page.on("response", maybe_add_network_response)

        logging.info("打开搜索页：%s", search_url)
        page.goto(search_url, wait_until="networkidle", timeout=90000)
        page.wait_for_timeout(int(delay * 1000))

        page_no = 0
        stale_rounds = 0
        while True:
            page_no += 1
            before_count = len(dedupe_links(collected + network_collected))

            # 滚动触发懒加载。
            for _ in range(4):
                page.evaluate("window.scrollTo(0, document.body.scrollHeight)")
                page.wait_for_timeout(int(delay * 1000))

            # 从 DOM 抽取所有链接。
            try:
                dom_links = page.eval_on_selector_all(
                    "a[href]",
                    """
                    els => els.map(a => ({
                        href: a.href,
                        text: (a.innerText || a.textContent || '').trim()
                    }))
                    """,
                )
            except Exception:
                dom_links = []

            for row in dom_links:
                u = row.get("href", "")
                t = clean_anchor_text(row.get("text", ""))
                if looks_like_article_url(u):
                    collected.append(LinkItem(url=u, anchor_text=t, found_from=f"dom-page-{page_no}"))

            current = dedupe_links(collected + network_collected)
            after_count = len(current)
            logging.info("搜索页轮次 %s：累计候选链接 %s 个", page_no, after_count)

            if max_results and after_count >= max_results:
                logging.info("达到 max_results=%s，停止搜索链接收集。", max_results)
                break
            if max_pages and page_no >= max_pages:
                logging.info("达到 max_pages=%s，停止搜索链接收集。", max_pages)
                break

            # 优先点击“下一页”类控件；兼容 a/button/li/span。
            clicked_next = False
            next_selectors = [
                "a:has-text('下一页')",
                "button:has-text('下一页')",
                "text=下一页",
                "a:has-text('下页')",
                ".next:not(.disabled)",
                "li.next:not(.disabled)",
                "[aria-label*='下一页']",
            ]
            for sel in next_selectors:
                try:
                    loc = page.locator(sel).last
                    if loc.count() > 0 and loc.is_visible(timeout=1200):
                        old_url = page.url
                        loc.click(timeout=5000)
                        try:
                            page.wait_for_load_state("networkidle", timeout=20000)
                        except PlaywrightTimeoutError:
                            pass
                        page.wait_for_timeout(int(delay * 1000))
                        clicked_next = True
                        logging.info("已点击下一页控件：%s；URL：%s -> %s", sel, old_url, page.url)
                        break
                except Exception:
                    continue

            if clicked_next:
                stale_rounds = 0
                continue

            # 有些页面是无限滚动：如果本轮没有新增，连续多轮后退出。
            if after_count <= before_count:
                stale_rounds += 1
            else:
                stale_rounds = 0

            if stale_rounds >= 3:
                logging.info("连续 %s 轮未发现新增链接，停止搜索链接收集。", stale_rounds)
                break

        browser.close()

    links = dedupe_links(collected + network_collected)
    if max_results:
        links = links[:max_results]
    logging.info("最终候选链接数：%s", len(links))
    return links


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": (
                "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                "AppleWebKit/537.36 (KHTML, like Gecko) "
                "Chrome/120.0.0.0 Safari/537.36"
            ),
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
        }
    )
    retry = Retry(
        total=4,
        connect=4,
        read=4,
        backoff_factor=0.8,
        status_forcelist=(429, 500, 502, 503, 504),
        allowed_methods=("GET", "HEAD"),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry, pool_connections=10, pool_maxsize=10)
    session.mount("http://", adapter)
    session.mount("https://", adapter)
    return session


def fetch_html(session: requests.Session, url: str, timeout: int = 30) -> Optional[str]:
    try:
        resp = session.get(url, timeout=timeout)
        if resp.status_code >= 400:
            logging.warning("请求失败 status=%s url=%s", resp.status_code, url)
            return None
        # 政府站旧页面可能未正确声明编码，apparent_encoding 更稳。
        if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
            resp.encoding = resp.apparent_encoding or "utf-8"
        return resp.text
    except Exception as e:
        logging.warning("请求异常 url=%s error=%s", url, e)
        return None


def remove_noise_nodes(soup: BeautifulSoup) -> None:
    for tag in soup(["script", "style", "noscript", "iframe", "svg", "canvas", "form"]):
        tag.decompose()
    for selector in [
        "header",
        "footer",
        "nav",
        ".header",
        ".footer",
        ".nav",
        ".crumb",
        ".breadcrumb",
        ".search",
        ".share",
    ]:
        for tag in soup.select(selector):
            # 不强制删除可能包含正文的 div，仅删除明显头尾导航。
            if len(tag.get_text("", strip=True)) < 1200:
                tag.decompose()


def get_visible_text(soup: BeautifulSoup) -> str:
    text = soup.get_text("\n", strip=True)
    return normalize_space(text)


def extract_title(soup: BeautifulSoup, fallback: str = "") -> str:
    title_selectors = [
        "h1",
        ".article-title",
        ".detail-title",
        ".content-title",
        ".news-title",
        ".title",
        "h2",
        "h3",
    ]
    for sel in title_selectors:
        node = soup.select_one(sel)
        if node:
            text = clean_anchor_text(node.get_text(" ", strip=True))
            if len(text) >= 4 and not re.search(r"站内搜索|网站首页|政府信息公开", text):
                return text

    for attr_sel in [
        ('meta[property="og:title"]', "content"),
        ('meta[name="ArticleTitle"]', "content"),
        ('meta[name="title"]', "content"),
    ]:
        node = soup.select_one(attr_sel[0])
        if node and node.get(attr_sel[1]):
            text = clean_anchor_text(str(node.get(attr_sel[1])))
            if text:
                return text

    if soup.title and soup.title.get_text(strip=True):
        text = soup.title.get_text(strip=True)
        text = re.sub(r"[-_—|｜].*$", "", text).strip()
        if text:
            return text
    return fallback.strip()


def normalize_date(date_text: str) -> str:
    date_text = clean_anchor_text(date_text)
    m = re.search(r"(\d{4})年(\d{1,2})月(\d{1,2})日(?:\s*(\d{1,2}:\d{2}))?", date_text)
    if m:
        y, mo, d, hm = m.groups()
        out = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        if hm:
            out += " " + hm
        return out
    m = re.search(r"(\d{4})[-./](\d{1,2})[-./](\d{1,2})(?:\s*(\d{1,2}:\d{2}))?", date_text)
    if m:
        y, mo, d, hm = m.groups()
        out = f"{int(y):04d}-{int(mo):02d}-{int(d):02d}"
        if hm:
            out += " " + hm
        return out
    return date_text


def extract_publish_time(text: str) -> str:
    patterns = [
        r"(?:发布时间|发布日期|发文日期|时间)[:：]\s*(\d{4}年\d{1,2}月\d{1,2}日(?:\s*\d{1,2}:\d{2})?)",
        r"(?:发布时间|发布日期|发文日期|时间)[:：]\s*(\d{4}[-./]\d{1,2}[-./]\d{1,2}(?:\s*\d{1,2}:\d{2})?)",
        r"(\d{4}年\d{1,2}月\d{1,2}日(?:\s*\d{1,2}:\d{2})?)",
        r"(\d{4}[-./]\d{1,2}[-./]\d{1,2}(?:\s*\d{1,2}:\d{2})?)",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            return normalize_date(m.group(1))
    return ""


def extract_source(text: str, url: str) -> str:
    # 显式字段优先。
    patterns = [
        r"(?:信息来源|来源)[:：]\s*([^\n\s][^\n]{0,60})",
        r"发布机构[:：]\s*([^\n\s][^\n]{0,60})",
        r"主办单位[:：]\s*([^\n\s][^\n]{0,60})",
    ]
    for pat in patterns:
        m = re.search(pat, text)
        if m:
            src = clean_anchor_text(m.group(1))
            src = re.split(r"\s+(?:发布时间|时间|发文日期)[:：]", src)[0].strip()
            src = re.sub(r"[〖【\[].*$", "", src).strip()
            if src and len(src) <= 80:
                return src

    # 兼容：湖南省工业和信息化厅 gxt.hunan.gov.cn 时间：2026年...
    m = re.search(r"([\u4e00-\u9fa5（）()·]{4,40})\s+(?:[a-z0-9.-]+\.)?hunan\.gov\.cn\s+时间[:：]", text, re.I)
    if m:
        return clean_anchor_text(m.group(1))

    # 不伪造来源：未命中显式来源字段时返回空字符串。
    return ""


def line_is_noise(line: str) -> bool:
    line = clean_anchor_text(line)
    if not line:
        return True
    for pat in NAV_NOISE_PATTERNS:
        if re.match(pat, line):
            return True
    if len(line) <= 2 and line not in {"一、", "二、", "三、"}:
        return True
    return False


def crop_stop_markers(text: str) -> str:
    earliest = len(text)
    for marker in STOP_TEXT_MARKERS:
        idx = text.find(marker)
        if idx != -1:
            earliest = min(earliest, idx)
    return text[:earliest].strip()


def score_content_text(text: str) -> tuple[int, int, int]:
    c = compact_cn(text)
    return (len(c), c.count(KEYWORD), text.count("\n"))


def extract_body(soup: BeautifulSoup, full_text: str, title: str) -> str:
    # 常见正文容器。按长度与关键词出现次数评分。
    selectors = [
        ".TRS_Editor",
        "#zoom",
        ".article-content",
        ".article_content",
        ".detail-content",
        ".detail_content",
        ".content",
        ".mainContent",
        ".news_content",
        ".xxgk_content",
        ".view",
        ".cont",
        "article",
        "main",
    ]
    candidates: list[str] = []
    for sel in selectors:
        for node in soup.select(sel):
            text = normalize_space(node.get_text("\n", strip=True))
            text = crop_stop_markers(text)
            if len(compact_cn(text)) >= 80:
                candidates.append(text)

    if candidates:
        candidates = sorted(candidates, key=score_content_text, reverse=True)
        body = candidates[0]
    else:
        # 兜底：从全文中裁切掉标题、面包屑、元数据和页脚。
        body = full_text
        body = crop_stop_markers(body)
        if title and title in body:
            body = body.split(title, 1)[-1]
        # 删除标题下方常见元数据行。
        body = re.sub(r"^[\s\S]{0,300}?(?:发布时间|时间|发文日期|发布日期)[:：][^\n]*\n", "", body, count=1)

    lines: list[str] = []
    seen: set[str] = set()
    for raw_line in body.split("\n"):
        line = clean_anchor_text(raw_line)
        if line_is_noise(line):
            continue
        # 删除残留元数据。
        if re.search(r"^(索引号|题材分类|主题分类|统一登记|主题词|文号|名称)[:：]", line):
            continue
        if re.search(r"^(湖南省工业和信息化厅\s+gxt\.hunan\.gov\.cn\s+时间|当前位置|您当前的位置)", line):
            continue
        if line in seen:
            continue
        seen.add(line)
        lines.append(line)

    body = "\n".join(lines).strip()
    body = crop_stop_markers(body)
    return normalize_space(body)


def parse_article(html: str, url: str, anchor_text: str = "") -> Optional[Article]:
    soup = BeautifulSoup(html, "lxml")
    remove_noise_nodes(soup)
    full_text = get_visible_text(soup)
    if not full_text:
        return None
    title = extract_title(soup, anchor_text)
    publish_time = extract_publish_time(full_text)
    source = extract_source(full_text, url)
    body = extract_body(soup, full_text, title)

    if not title:
        logging.warning("标题缺失，保留空字段：%s", url)
    if not publish_time:
        logging.warning("发布时间缺失，保留空字段：%s", url)
    if not source:
        logging.warning("信息来源缺失，保留空字段：%s", url)

    combined_compact = compact_cn(title + "\n" + body)
    if KEYWORD not in combined_compact:
        # 避免站内搜索把“低空”和“经济”拆词后抓到泛经济类噪声。
        return None

    if len(compact_cn(body)) < 40:
        logging.warning("正文过短，跳过：%s title=%s", url, title)
        return None

    return Article(
        index=0,
        title=title,
        publish_time=publish_time,
        source=source,
        url=url,
        body=body,
        body_chars=len(compact_cn(body)),
        collected_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
    )


def save_links_csv(links: list[LinkItem], output_dir: Path) -> None:
    path = output_dir / "links_raw.csv"
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=["url", "anchor_text", "found_from"])
        writer.writeheader()
        for item in links:
            writer.writerow(asdict(item))
    logging.info("已输出候选链接表：%s", path)


def save_articles(articles: list[Article], output_dir: Path) -> None:
    output_dir.mkdir(parents=True, exist_ok=True)

    jsonl_path = output_dir / "articles.jsonl"
    with jsonl_path.open("w", encoding="utf-8") as f:
        for art in articles:
            f.write(json.dumps(asdict(art), ensure_ascii=False) + "\n")

    csv_path = output_dir / "articles.csv"
    pd.DataFrame([asdict(a) for a in articles]).to_csv(csv_path, index=False, encoding="utf-8-sig")

    txt_path = output_dir / "hunan_low_altitude_fulltext.txt"
    with txt_path.open("w", encoding="utf-8") as f:
        f.write(f"检索词：{KEYWORD}\n")
        f.write(f"采集时间：{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
        f.write(f"有效文章数：{len(articles)}\n")
        f.write("\n")
        for art in articles:
            f.write("=" * 90 + "\n")
            f.write(f"序号：{art.index}\n")
            f.write(f"标题：{art.title}\n")
            f.write(f"发布时间：{art.publish_time}\n")
            f.write(f"信息来源：{art.source}\n")
            f.write(f"原文链接：{art.url}\n")
            f.write("正文：\n")
            f.write(art.body.strip() + "\n\n")

    logging.info("已输出 JSONL：%s", jsonl_path)
    logging.info("已输出 CSV：%s", csv_path)
    logging.info("已输出 TXT：%s", txt_path)


def crawl_articles(links: list[LinkItem], output_dir: Path, delay: float) -> list[Article]:
    session = build_session()
    articles: list[Article] = []
    for i, link in enumerate(links, start=1):
        try:
            logging.info("[%s/%s] 抓取详情页：%s", i, len(links), link.url)
            html = fetch_html(session, link.url)
            if not html:
                logging.warning("详情页抓取失败，已跳过：%s", link.url)
                continue

            art = parse_article(html, link.url, link.anchor_text)
            if art:
                art.index = len(articles) + 1
                articles.append(art)
                logging.info("保留：%s | %s | %s 字", art.publish_time, art.title, art.body_chars)
            else:
                logging.info("跳过：未通过相关性或正文质量过滤。")
        except Exception as exc:
            logging.exception("详情页处理异常，已跳过。url=%s error=%s", link.url, exc)
        finally:
            # 保守限速，避免对政府网站造成压力。失败场景也保持间隔。
            time.sleep(delay + random.uniform(0, delay * 0.5))

        # 断点式中间保存，避免长任务中断后完全丢失。
        if len(articles) > 0 and len(articles) % 20 == 0:
            save_articles(articles, output_dir)

    return articles


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抓取湖南省工信厅站内搜索‘低空经济’相关新闻/政策全文。")
    parser.add_argument("--search-url", default=SEARCH_URL, help="搜索页 URL")
    parser.add_argument("--output-dir", default="output", help="输出目录")
    parser.add_argument("--delay", type=float, default=1.2, help="详情页请求间隔秒数，建议 >=1")
    parser.add_argument("--max-pages", type=int, default=0, help="搜索页最多翻页/加载轮数；0 表示不设上限，由页面耗尽决定")
    parser.add_argument("--max-results", type=int, default=0, help="最多处理候选链接数；0 表示不设上限")
    parser.add_argument("--headless", action="store_true", help="无头浏览器模式")
    parser.add_argument("--show-browser", action="store_true", help="显示浏览器窗口，便于调试；会覆盖 --headless")
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    output_dir = Path(args.output_dir)
    setup_logging(output_dir)

    if args.delay < 1.2:
        logging.error("参数 delay=%.3f 低于允许下限 1.2，请使用 --delay >= 1.2。", args.delay)
        raise SystemExit(2)

    headless = args.headless and not args.show_browser
    logging.info("启动采集任务。search_url=%s", args.search_url)
    logging.info("参数：headless=%s delay=%s max_pages=%s max_results=%s", headless, args.delay, args.max_pages, args.max_results)

    links = collect_search_links(
        search_url=args.search_url,
        max_pages=args.max_pages,
        max_results=args.max_results,
        headless=headless,
        delay=args.delay,
    )
    save_links_csv(links, output_dir)

    articles = crawl_articles(links, output_dir, delay=args.delay)
    save_articles(articles, output_dir)
    logging.info("任务完成。有效文章数：%s；候选链接数：%s", len(articles), len(links))


if __name__ == "__main__":
    main()
