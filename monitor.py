# -*- coding: utf-8 -*-
"""
GitHub 新项目监控 -> 邮件日报

设计要点（对应原版失效的地方）：
1. 每个监控项一个 query，query 从 GitHub Actions 的 vars 读取，读不到就用内置默认值。
   原版 vars 未配置时 query 变成空串，Search API 直接 422 或者返回一堆无关仓库。
2. query 用 requests 的 params 传，不手工拼 URL。
3. 历史记录（last_*.txt）只增不减：见过就不再报。原版每次覆盖写，一次 API 失败
   或者返回空，历史就被清空，第二天全部当新增再报一遍。
4. 单个任务失败不影响其他任务，带退避重试。
5. 同时产出纯文本和 HTML 两个版本，HTML 走邮件更清楚。
6. 本地也能跑：没有 GITHUB_OUTPUT 时把结果写到 out/ 目录。
"""

import json
import os
import random
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import quote

import requests

API = "https://api.github.com/search/repositories"
UA = "github-new-repo-monitor/2.0"

GH_TOKEN = (os.getenv("GH_TOKEN") or "").strip()

FETCH_COUNT = int(os.getenv("FETCH_COUNT") or 50)
MIN_STAR = int(os.getenv("MIN_STAR") or 30)
MAX_STAR = int(os.getenv("MAX_STAR") or 2500)
PER_TASK_LIMIT = int(os.getenv("PER_TASK_LIMIT") or 15)   # 单封邮件里每个分类最多列几条
FRESH_DAYS = int(os.getenv("FRESH_DAYS") or 120)          # 只报最近多少天内创建的仓库（首次跑等于回填）
MIN_HITS = int(os.getenv("MIN_HITS") or 5)                # 单次拉取少于这么多条就触发兜底 query
HISTORY_KEEP = int(os.getenv("HISTORY_KEEP") or 8000)     # 历史 id 上限，防止文件无限膨胀
REQUEST_TIMEOUT = int(os.getenv("REQUEST_TIMEOUT") or 30)
RETRY_TIMES = int(os.getenv("RETRY_TIMES") or 3)

HISTORY_DIR = Path(os.getenv("HISTORY_DIR") or "history")
OUT_DIR = Path(os.getenv("OUT_DIR") or "out")

# 全局黑名单：命中 full_name + description + topics 任一处就丢弃。
# 注意别往这里塞 scrape / crawler —— 第二个分类本身就是爬虫管线，会把自己整类干掉。
BLACKLIST_KEYWORDS = {
    # 中文
    "短剧", "红果", "解析", "爬虫", "抓取", "破解", "vip解析",
    "影视解析", "视频抓取", "douyin抓取", "tiktok爬虫", "搬运", "刷量",
    # 英文（只在仓库名/简介里精确匹配这些短语，避免误杀正常采集类项目）
    "vip video", "video parser", "movie parser", "drama downloader",
    "crack version", "keygen", "key checker", "license bypass",
    "free vip", "unlimited vip", "观看vip",
}

# 明显是垃圾/空壳/重复搬运的仓库特征
BLACKLIST_RE = re.compile(
    r"(^|[/\-\_])(test|demo|tutorial|example|hello-world|my-first-repo)($|[/\-\_])",
    re.I,
)

# query 的内置默认值：env 变量名 -> (主 query, 兜底 query)
# GitHub Search 的裸关键词之间是 AND 关系，堆 5 个词经常 0 结果，
# 所以每个分类都配一条更短的核心词兜底。
QUERY_DEFAULTS = {
    "SEARCH_QUERY_AGENT": (
        "agent framework language:python",
        "agent language:python",
    ),
    "SEARCH_QUERY_DOC_PIPELINE": (
        "document pipeline pdf markdown language:python",
        "pdf markdown language:python",
    ),
    "SEARCH_QUERY_TABLE": (
        "excel csv data analysis language:python",
        "excel language:python",
    ),
    "SEARCH_QUERY_RAG_KB": (
        "rag knowledge base language:python",
        "rag language:python",
    ),
    "SEARCH_QUERY_SCRIPT": (
        "video script agent llm",
        "script agent llm",
    ),
    "SEARCH_QUERY_WEB_DESIGN": (
        "cms builder editor language:typescript",
        "cms language:typescript",
    ),
    "SEARCH_QUERY_EDUCATION": (
        "exam quiz language:python",
        "exam language:python",
    ),
    "SEARCH_QUERY_LIVE_TOOLS": (
        "subtitle language:python",
        "live dashboard language:typescript",
    ),
    "SEARCH_QUERY_OFFICE_AUTO": (
        "office automation pdf ocr",
        "pdf ocr",
    ),
}

WATCH_TASKS = [
    {"name": "一、LLM / Agent 智能体方向", "env": "SEARCH_QUERY_AGENT", "file": "last_agent.txt"},
    {"name": "二、资料采集清洗整理（网页/PDF文档RAG管线）", "env": "SEARCH_QUERY_DOC_PIPELINE", "file": "last_doc_pipeline.txt"},
    {"name": "三、Excel/CSV表格、位次数据清洗", "env": "SEARCH_QUERY_TABLE", "file": "last_table.txt"},
    {"name": "四、RAG知识库构建、文档入库", "env": "SEARCH_QUERY_RAG_KB", "file": "last_rag_kb.txt"},
    {"name": "五、短视频/直播文案生成Agent", "env": "SEARCH_QUERY_SCRIPT", "file": "last_script_agent.txt"},
    {"name": "六、网站、前端、CMS、设计工具", "env": "SEARCH_QUERY_WEB_DESIGN", "file": "last_web_design.txt"},
    {"name": "七、开源教育平台、志愿/题库系统", "env": "SEARCH_QUERY_EDUCATION", "file": "last_education.txt"},
    {"name": "八、新媒体直播工具（提词器、字幕、直播看板）", "env": "SEARCH_QUERY_LIVE_TOOLS", "file": "last_live_tools.txt"},
    {"name": "九、自动化办公、PDF/OCR/RPA", "env": "SEARCH_QUERY_OFFICE_AUTO", "file": "last_office_auto.txt"},
]


def log(*args):
    print(*args, file=sys.stderr, flush=True)


def build_query(raw: str) -> str:
    """补默认时间窗口：只保留最近 FRESH_DAYS 天内创建的仓库。

    这里必须是 created: 而不是 pushed:。监控的是「新项目」，pushed: 会把
    一年前创建、昨天提交过的老仓库当新项目报出来。用户自己写了 created:/
    pushed: 就尊重用户的写法，不覆盖。
    """
    q = (raw or "").strip()
    if not q:
        return ""
    if "created:" not in q and "pushed:" not in q:
        since = (datetime.now(timezone.utc) - timedelta(days=FRESH_DAYS)).strftime("%Y-%m-%d")
        q = f"{q} created:>{since}"
    if "fork:" not in q:
        q = f"{q} fork:false"
    # star 必须交给服务端过滤。否则 API 按 created 倒序只回最新 50 条，
    # 新仓库大多还没涨到 30 星，本地再怎么筛都是 0。
    #
    # 注意：不能写 "stars:>=30 stars:<=2500" —— 两个 stars: 限定符同时出现时
    # GitHub 只认后一个，会把 0 star 的仓库全放进来。必须用区间语法 30..2500。
    if "stars:" not in q:
        q = f"{q} stars:{MIN_STAR}..{MAX_STAR}"
    return q


def repo_text(repo) -> str:
    parts = [
        repo.get("full_name") or "",
        repo.get("description") or "",
        " ".join(repo.get("topics") or []),
    ]
    return " ".join(parts)


def is_blacklisted(repo) -> bool:
    text = repo_text(repo).lower()
    for kw in BLACKLIST_KEYWORDS:
        if kw.lower() in text:
            return True
    if BLACKLIST_RE.search(repo.get("full_name") or ""):
        return True
    return False


def star_in_range(repo) -> bool:
    try:
        st = int(repo.get("stargazers_count") or 0)
    except (TypeError, ValueError):
        return False
    return MIN_STAR <= st <= MAX_STAR


def fetch_repos(query: str):
    """调 Search API，403/429/5xx 退避重试。

    返回 list 表示请求成功（可能是空列表，那只是真的没有结果）；
    返回 None 表示请求失败，调用方需要区分这两种情况。
    """
    headers = {"Accept": "application/vnd.github+json", "User-Agent": UA}
    if GH_TOKEN:
        headers["Authorization"] = f"Bearer {GH_TOKEN}"

    params = {
        "q": query,
        "sort": "created",
        "order": "desc",
        "per_page": max(1, min(FETCH_COUNT, 100)),
    }

    last_err = None
    for attempt in range(1, RETRY_TIMES + 1):
        try:
            resp = requests.get(API, headers=headers, params=params, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as e:
            last_err = f"网络异常: {e}"
            log(f"  [retry {attempt}/{RETRY_TIMES}] {last_err}")
            time.sleep(min(2 ** attempt, 30))
            continue

        if resp.status_code == 200:
            return resp.json().get("items", []) or []

        body = (resp.text or "")[:300]
        last_err = f"HTTP {resp.status_code}: {body}"

        # 401/422 是配置问题，重试没用
        if resp.status_code in (401, 422):
            log(f"  [fail] {last_err}")
            log(f"  query = {query!r}")
            return None

        if resp.status_code in (403, 429):
            # 区分「限流」和「真的没权限」
            if "rate limit" in body.lower() or resp.headers.get("x-ratelimit-remaining") == "0":
                reset = resp.headers.get("x-ratelimit-reset")
                wait = 60
                if reset and reset.isdigit():
                    wait = max(5, min(int(reset) - int(time.time()) + 2, 120))
                log(f"  [retry {attempt}/{RETRY_TIMES}] 限流，等待 {wait}s")
                time.sleep(wait)
            else:
                log(f"  [fail] {last_err}")
                return None
            continue

        log(f"  [retry {attempt}/{RETRY_TIMES}] {last_err}")
        time.sleep(min(2 ** attempt, 30))

    log(f"  [give up] {last_err}")
    return None


def load_seen(path: Path) -> set:
    if not path.exists():
        return set()
    try:
        raw = path.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return set()
    return {line.strip() for line in raw.splitlines() if line.strip()}


def save_seen(path: Path, seen: set):
    """只增不减地写回，保留最近 HISTORY_KEEP 个 id。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    ordered = sorted(seen, key=lambda x: (len(x), x), reverse=True)[:HISTORY_KEEP]
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text("\n".join(ordered) + "\n", encoding="utf-8")
    os.replace(tmp, path)


def fmt_star(n) -> str:
    n = int(n or 0)
    return f"{n / 1000:.1f}k" if n >= 1000 else str(n)


def fmt_date(s) -> str:
    if not s:
        return "未知"
    try:
        return datetime.strptime(s[:10], "%Y-%m-%d").strftime("%Y-%m-%d")
    except ValueError:
        return s[:10]


def esc(s: str) -> str:
    return (
        (s or "")
        .replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def render_text(title: str, sections) -> str:
    lines = [title, "=" * 46, ""]
    for task_name, items in sections:
        lines.append(task_name)
        lines.append("-" * 30)
        if not items:
            lines.append("  今日无新增")
        else:
            for idx, r in enumerate(items, 1):
                lines.append(f"{idx}. {r['full_name']}  ★{fmt_star(r.get('stargazers_count'))}")
                lines.append(f"   语言：{r.get('language') or '未标注'}   创建：{fmt_date(r.get('created_at'))}")
                desc = (r.get("description") or "无简介").strip()
                if len(desc) > 160:
                    desc = desc[:157] + "..."
                lines.append(f"   简介：{desc}")
                lines.append(f"   链接：{r.get('html_url')}")
                lines.append("")
        lines.append("")
    return "\n".join(lines)


def render_html(title: str, sections, generated_at: str, failed) -> str:
    total = sum(len(i) for _, i in sections)
    h = [
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'font-size:14px;line-height:1.6;color:#24292f;max-width:760px">',
        f'<h2 style="margin:0 0 4px">{esc(title)}</h2>',
        f'<p style="margin:0 0 18px;color:#57606a">生成时间 {esc(generated_at)} · '
        f'共 <b>{total}</b> 个新项目 · star 区间 {MIN_STAR}–{MAX_STAR}</p>',
    ]
    if failed:
        h.append(
            '<p style="background:#fff8c5;border-left:3px solid #d4a72c;padding:8px 12px;'
            'margin:0 0 16px;color:#7d4e00">以下分类本次拉取失败，未包含结果：'
            + esc("、".join(failed))
            + "</p>"
        )
    for task_name, items in sections:
        h.append(
            f'<h3 style="margin:22px 0 8px;font-size:15px;border-bottom:1px solid #d0d7de;'
            f'padding-bottom:5px">{esc(task_name)}</h3>'
        )
        if not items:
            h.append('<p style="margin:6px 0;color:#57606a">今日无新增</p>')
            continue
        h.append('<ol style="margin:6px 0 0;padding-left:22px">')
        for r in items:
            name = esc(r.get("full_name") or "")
            url = esc(r.get("html_url") or "#")
            desc = (r.get("description") or "无简介").strip()
            if len(desc) > 180:
                desc = desc[:177] + "..."
            lang = esc(r.get("language") or "未标注")
            h.append(
                f'<li style="margin-bottom:10px">'
                f'<a href="{url}" style="color:#0969da;font-weight:600;text-decoration:none">{name}</a> '
                f'<span style="color:#bf8700">★{fmt_star(r.get("stargazers_count"))}</span> '
                f'<span style="color:#57606a;font-size:12px">· {lang} · '
                f'{esc(fmt_date(r.get("created_at")))}</span>'
                f'<div style="color:#57606a;font-size:13px;margin-top:2px">{esc(desc)}</div>'
                f"</li>"
            )
        h.append("</ol>")
    h.append(
        '<p style="margin-top:26px;padding-top:10px;border-top:1px solid #d0d7de;'
        'color:#8c959f;font-size:12px">GitHub 新项目监控 · 自动发送</p>'
    )
    h.append("</div>")
    return "\n".join(h)


def write_output(name: str, value: str):
    """GitHub Actions 多行 output，用随机分隔符避免内容里出现 EOF 撞车。"""
    gh_out = os.getenv("GITHUB_OUTPUT")
    if not gh_out:
        return False
    delim = "EOF_" + uuid.uuid4().hex
    with open(gh_out, "a", encoding="utf-8") as f:
        f.write(f"{name}<<{delim}\n{value}\n{delim}\n")
    return True


def main():
    if not GH_TOKEN:
        log("警告：GH_TOKEN 未配置，走未认证调用（60 次/小时，容易被限流）")

    HISTORY_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc).astimezone()
    generated_at = now.strftime("%Y-%m-%d %H:%M")

    sections = []
    failed = []
    stats = []
    claimed = set()   # 跨分类去重：同一个仓库只出现在最靠前那个分类里

    for task in WATCH_TASKS:
        raw = (os.getenv(task["env"]) or "").strip()
        source = "vars" if raw else "默认值"
        primary, fallback = QUERY_DEFAULTS[task["env"]]

        query = build_query(raw or primary)
        log(f"[{task['name']}] query({source}) = {query}")

        items = fetch_repos(query)
        fetch_failed = items is None
        if fetch_failed:
            items = []

        # 主 query 命中太少就用短词兜底再拉一次，避免整类空白
        if not fetch_failed and len(items) < MIN_HITS and not raw:
            fb = build_query(fallback)
            log(f"  主 query 仅 {len(items)} 条，用兜底 query 重试：{fb}")
            more = fetch_repos(fb)
            if more:
                merged = {str(r.get("id")): r for r in items}
                merged.update({str(r.get("id")): r for r in more})
                items = list(merged.values())
                log(f"  合并后 {len(items)} 条")

        filtered = [r for r in items if not is_blacklisted(r) and star_in_range(r)]

        hist_path = HISTORY_DIR / task["file"]
        seen = load_seen(hist_path)

        new_items = []
        for r in filtered:
            rid = str(r.get("id"))
            if rid in seen or rid in claimed:
                continue
            new_items.append(r)
            claimed.add(rid)

        # 历史只增：本次看到的全部记为已见
        seen.update(str(r.get("id")) for r in filtered if r.get("id"))
        try:
            save_seen(hist_path, seen)
        except OSError as e:
            log(f"  [warn] 写历史失败 {hist_path}: {e}")

        new_items.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        new_items = new_items[:PER_TASK_LIMIT]

        if fetch_failed:
            failed.append(task["name"])

        stats.append((task["name"], len(items), len(filtered), len(new_items)))
        sections.append((task["name"], new_items))
        log(f"  拉到 {len(items)} / 过滤后 {len(filtered)} / 新增 {len(new_items)}"
            + ("  [拉取失败]" if fetch_failed else ""))

        time.sleep(0.5)   # 温和一点，别把 Search API 配额打穿

    title = f"GitHub 新项目监控日报 {now.strftime('%Y-%m-%d')}"
    text_body = render_text(title, sections)
    html_body = render_html(title, sections, generated_at, failed)

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    (OUT_DIR / "mail.txt").write_text(text_body, encoding="utf-8")
    (OUT_DIR / "mail.html").write_text(html_body, encoding="utf-8")
    (OUT_DIR / "stats.json").write_text(
        json.dumps(
            [{"task": n, "fetched": a, "filtered": b, "new": c} for n, a, b, c in stats],
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )

    ok = write_output("mail_body", html_body)
    write_output("mail_text", text_body)
    write_output("has_new", "true" if any(n for _, _, _, n in stats) else "false")
    write_output("subject_date", now.strftime("%Y-%m-%d"))

    print("===== 邮件正文（纯文本预览）=====")
    print(text_body)
    if not ok:
        log(f"\n[local] GITHUB_OUTPUT 未设置，结果已写入 {OUT_DIR.resolve()}")

    total_new = sum(c for _, _, _, c in stats)
    log(f"\n完成：{total_new} 个新项目" + (f"，失败分类 {len(failed)} 个" if failed else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main())
