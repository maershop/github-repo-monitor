# -*- coding: utf-8 -*-
"""
GitHub 新项目监控 -> 邮件日报

设计要点（对应原版失效的地方）：
1. 每个监控项一个 query，query 从 GitHub Actions 的 vars 读取，读不到就用内置默认值。
   原版 vars 未配置时 query 变成空串，Search API 直接 422 或者返回一堆无关仓库。
2. query 用标准库 urlencode 传参，不手工拼 URL。全脚本零第三方依赖（只用标准库），
   这样 Actions 上不需要 pip install，也就不存在依赖文件缺失导致失败的问题。
3. 历史记录（last_*.txt）只增不减：见过就不再报。原版每次覆盖写，一次 API 失败
   或者返回空，历史就被清空，第二天全部当新增再报一遍。
4. 单个任务失败不影响其他任务，带退避重试。
5. 同时产出纯文本和 HTML 两个版本，HTML 走邮件更清楚。
6. 本地也能跑：没有 GITHUB_OUTPUT 时把结果写到 out/ 目录。
"""

import json
import os
import re
import sys
import time
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlencode
from urllib.request import Request, urlopen

API = "https://api.github.com/search/repositories"
UA = "github-new-repo-monitor/2.0"
# 版本号：跑起来第一行会打印，用来确认 GitHub 上跑的到底是哪一版。
# 之前反复出现「线上还是旧脚本」的问题，靠日志一眼就能看出来。
VERSION = "3.0"

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

# 是否过滤掉「商业软件破解/repack 分发」仓库。
# 默认 0 = 保留（这类仓库本身就是想找的资源）。
# 设 BLOCK_WAREZ=1 才过滤。
BLOCK_WAREZ = os.getenv("BLOCK_WAREZ", "0").strip().lower() in ("1", "true", "yes", "on")

HISTORY_DIR = Path(os.getenv("HISTORY_DIR") or "history")
OUT_DIR = Path(os.getenv("OUT_DIR") or "out")

# 全局黑名单：命中 full_name + description + topics 任一处就丢弃。
# 注意别往这里塞 scrape / crawler —— 第二个分类本身就是爬虫管线，会把自己整类干掉。
#
# 这里只放「内容垃圾」类（短剧搬运、刷量、空壳 demo），不放「破解/资源分发」
# 类 —— 那类恰恰是想找的目标，由 BLOCK_WAREZ 开关单独控制。
BLACKLIST_KEYWORDS = {
    # 中文
    "红果", "搬运", "刷量", "短剧推广",
    # 英文（只在仓库名/简介里精确匹配这些短语，避免误杀正常采集类项目）
    "drama downloader", "video parser", "movie parser",
}

# 「资源发布/破解」类关键词：默认不过滤，BLOCK_WAREZ=1 时才启用。
# 单独一组，避免想保留资源的人被自己的黑名单误伤。
WAREZ_KEYWORDS = {
    "破解", "解析", "vip解析", "影视解析", "视频抓取", "douyin抓取", "tiktok爬虫",
    "vip video", "crack version", "keygen", "key checker", "license bypass",
    "free vip", "unlimited vip", "观看vip", "激活", "绿色版",
}

# 明显是垃圾/空壳/重复搬运的仓库特征
BLACKLIST_RE = re.compile(
    r"(^|[/\-\_])(test|demo|tutorial|example|hello-world|my-first-repo)($|[/\-\_])",
    re.I,
)

# 商业软件「破解分发」仓库的识别。
#
# 这类仓库怎么混进来的：查询词里有 pdf / ocr / office 这种词时，GitHub 会
# 返回一堆 "Acrobat Pro for Windows"、"ABBYY FineReader PDF" 的 repack 仓库
# —— 名字里带正版商品名，简介写 "professional PDF editor ... OCR ..."，
# 看着完全符合「自动化办公」的需求，实际是盗版安装包分发页。
# 中文黑名单里的「破解」抓不到它们，必须按商品名 + 分发词两个维度一起判。
#
# 只匹配商品名还不够：正经项目也可能提到 "PDFelement 对比测试"。所以要求
# 「商品名」和「分发词」同时出现，才判定为盗版分发。
WAREZ_BRANDS = re.compile(
    # 注意品牌名里常有连字符/下划线：实测 orakio1975/acrobat-pro-pdf-editor
    # 写成 acrobat-pro，用 \s* 匹配不到，必须 [\s\-_]*。
    # ABBYY 也常被省略成 abby（ALIASLUBUSU/abby-pdf-transformer）。
    r"acrobat[\s\-_]*pro|adobe[\s\-_]*acrobat|abbyy?|finereader|pdfelement|wondershare|"
    r"nitro[\s\-_]*pdf|foxit|readiris|office[\s\-_]*20(1[6-9]|2[1-9])|"
    r"idm[\s\-_]*(crack|patch)|matlab[\s\-_]*20|autocad[\s\-_]*20|photoshop[\s\-_]*20|"
    r"coreldraw|sketchup[\s\-_]*pro|solidworks[\s\-_]*20",
    re.I,
)
WAREZ_DISTRO = re.compile(
    r"free\s*download|full\s*version|pre[\s\-]?activated|activator|activation\s*key|"
    r"license\s*key|serial\s*key|with\s*key|crack(ed|s)?\b|patch(ed)?\b|keygen|"
    r"repack|portable\s*edition|torrent|免[费费]|破解|激活|绿色版|安装包|\bwin(dows)?\s*(x64|10/11)\b",
    re.I,
)


def is_warez(repo) -> bool:
    """识别商业软件的盗版分发仓库。

    判定依据（按优先级）：
    1. 仓库名里直接带商业软件名 —— 正常开源项目不会把自己叫 Acrobat/ABBYY。
       这是最强的信号，实测仓库名 xx/acrobat-pro-pdf-editor 这类 100% 是 repack。
    2. 品牌名 + 分发词同时出现在名字/简介/topics 里。
    """
    full_name = repo.get("full_name") or ""
    text = repo_text(repo)
    # 1) 仓库名（去掉 owner）命中品牌名就直接丢
    name_only = full_name.split("/")[-1] if "/" in full_name else full_name
    if WAREZ_BRANDS.search(name_only):
        return True
    # 2) 描述里品牌 + 分发词的组合
    return bool(WAREZ_BRANDS.search(text) and WAREZ_DISTRO.search(text))

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
    """只返回 query 字符串。需要同时拿到生效的 star 区间用 build_query_ex。"""
    return build_query_ex(raw)[0]


def build_query_ex(raw: str):
    """返回 (query, lo, hi)。lo/hi 是本地复筛要用的生效区间。

    服务端按 lo..hi 返回，本地必须用同一个区间复筛。之前本地写死
    MIN_STAR..MAX_STAR，用户配了 stars:100..500 时，服务端只回 100~500 星的，
    本地再用 30..2500 筛虽然能过，但用户配 stars:<10 时本地会把全部结果筛空。
    """
    q = (raw or "").strip()
    if not q:
        return "", MIN_STAR, MAX_STAR
    # fork 限定符只能有一个，写法还分 fork:true / fork:false / fork:only 和
    # 否定式 -fork:true。这些混着写时 GitHub 行为不可预期，统一摘掉再补一条。
    q = _FORK_TOKEN.sub(" ", q)
    q = " ".join(q.split())
    if "created:" not in q:
        # 注意：即使用户写了 pushed: 也照样补 created:。
        # pushed:>2025-01-01 的意思是「最近提交过」，会把 2024 年建的老仓库
        # 当新项目报出来 —— 这不是「新项目监控」。两条同时写是交集，更准。
        since = (datetime.now(timezone.utc) - timedelta(days=FRESH_DAYS)).strftime("%Y-%m-%d")
        q = f"{q} created:>{since}"
    q = f"{q} fork:false"
    # star 必须交给服务端过滤。否则 API 按 created 倒序只回最新 50 条，
    # 新仓库大多还没涨到 30 星，本地再怎么筛都是 0。
    #
    # 注意：不能写 "stars:>=30 stars:<=2500" —— 两个 stars: 限定符同时出现时
    # GitHub 只认后一个，会把 0 star 的仓库全放进来。必须用区间语法 30..2500。
    #
    # 用户自己配的 stars: 也不能照抄：常见写法就是 "stars:>30 stars:<2500"，
    # 正好踩中上面的坑。所以这里把用户写的 stars: 全部摘掉，用它给出的上下界
    # 和我们自己的 MIN/MAX 取交集，最后只吐一个区间限定符。
    q, lo, hi = _merge_star_bounds(q)
    return f"{q} stars:{lo}..{hi}", lo, hi


# stars:>=100 / stars:>30 / stars:<2500 / stars:100..500 这类片段
_STAR_TOKEN = re.compile(r"(?<![\w-])stars:\s*([^\s]+)", re.IGNORECASE)

# fork:true / fork:false / fork:only / -fork:true / -fork:only
_FORK_TOKEN = re.compile(r"(?<![\w-])-?fork:\s*(?:true|false|only)\b", re.IGNORECASE)


def _merge_star_bounds(q: str):
    """摘掉 query 里所有 stars: 限定符，返回 (清理后的 query, lo, hi)。

    多个限定符按「交集」处理：取所有下界的最大值、所有上界的最小值。
    这样用户写的 stars:>30 stars:<2500 等价于 stars:31..2499，不会互相顶掉。

    用户完全没写 stars: 时才用 MIN_STAR/MAX_STAR 兜底。用户写了就听用户的，
    只补上他没说的那一侧 —— 否则用户写 stars:<10 会被我们的 MIN_STAR=30
    顶成不可能区间，整类直接空掉。
    """
    user_lo, user_hi = [], []
    for raw in _STAR_TOKEN.findall(q):
        bound_lo, bound_hi = _parse_star_spec(raw)
        if bound_lo is not None:
            user_lo.append(bound_lo)
        if bound_hi is not None:
            user_hi.append(bound_hi)

    if user_lo or user_hi:
        lo = max(user_lo) if user_lo else 0
        hi = min(user_hi) if user_hi else MAX_STAR
        if lo > hi:
            # 用户自己给了自相矛盾的上界和下界，无法猜测意图，退回默认。
            log(f"[warn] stars 区间自相矛盾（lo={lo} > hi={hi}），已退回默认 {MIN_STAR}..{MAX_STAR}")
            lo, hi = MIN_STAR, MAX_STAR
    else:
        lo, hi = MIN_STAR, MAX_STAR

    cleaned = _STAR_TOKEN.sub(" ", q)
    return " ".join(cleaned.split()), lo, hi


def _parse_star_spec(spec: str):
    """把单个 stars: 的值解析成 (下界, 上界)。

    支持 >=100、>30、<=2500、<1000、100..500、100、100..*、*..500。
    无法识别的写法返回 (None, None)，等于忽略该片段。
    """
    s = spec.strip().lower()
    if ".." in s:
        left, _, right = s.partition("..")
        lo = _to_int(left)
        hi = None if right in ("", "*") else _to_int(right)
        return lo, hi
    if s.startswith(">="):
        return _to_int(s[2:]), None
    if s.startswith("<="):
        return None, _to_int(s[2:])
    if s.startswith(">"):
        n = _to_int(s[1:])
        return (None if n is None else n + 1), None
    if s.startswith("<"):
        n = _to_int(s[1:])
        return None, (None if n is None else max(n - 1, 0))
    n = _to_int(s)
    return (n, n) if n is not None else (None, None)


def _to_int(text: str):
    try:
        return int(str(text).strip())
    except (TypeError, ValueError):
        return None


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
    # 破解/repack 仓库默认保留（BLOCK_WAREZ=0），只在显式开启时才丢。
    if BLOCK_WAREZ:
        if is_warez(repo):
            return True
        for kw in WAREZ_KEYWORDS:
            if kw.lower() in text:
                return True
    return False


def star_in_range(repo, lo=None, hi=None) -> bool:
    """本地复筛。lo/hi 必须用 build_query_ex 返回的生效区间，别写死全局常量。"""
    try:
        st = int(repo.get("stargazers_count") or 0)
    except (TypeError, ValueError):
        return False
    lo = MIN_STAR if lo is None else lo
    hi = MAX_STAR if hi is None else hi
    return lo <= st <= hi


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

    url = API + "?" + urlencode(params)

    last_err = None
    for attempt in range(1, RETRY_TIMES + 1):
        try:
            req = Request(url, headers=headers, method="GET")
            with urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                raw = resp.read().decode("utf-8", errors="replace")
            return json.loads(raw).get("items", []) or []
        except HTTPError as e:
            body = ""
            try:
                body = (e.read() or b"").decode("utf-8", errors="replace")[:300]
            except Exception:
                pass
            status = e.code
            last_err = f"HTTP {status}: {body}"

            # 401/422 是配置问题，重试没用
            if status in (401, 422):
                log(f"  [fail] {last_err}")
                log(f"  query = {query!r}")
                return None

            if status in (403, 429):
                # 区分「限流」和「真的没权限」
                hdr = {k.lower(): v for k, v in (e.headers or {}).items()}
                reset = hdr.get("x-ratelimit-reset")
                if "rate limit" in body.lower() or hdr.get("x-ratelimit-remaining") == "0":
                    wait = 60
                    if reset and str(reset).isdigit():
                        wait = max(5, min(int(reset) - int(time.time()) + 2, 120))
                    log(f"  [retry {attempt}/{RETRY_TIMES}] 限流，等待 {wait}s")
                    time.sleep(wait)
                    continue
                log(f"  [fail] {last_err}")
                return None

            log(f"  [retry {attempt}/{RETRY_TIMES}] {last_err}")
            time.sleep(min(2 ** attempt, 30))
            continue
        except (URLError, TimeoutError, OSError, ValueError) as e:
            last_err = f"网络异常: {e}"
            log(f"  [retry {attempt}/{RETRY_TIMES}] {last_err}")
            time.sleep(min(2 ** attempt, 30))
            continue

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
    for task_name, items, note in sections:
        lines.append(task_name + (f"  [{note}]" if note else ""))
        lines.append("-" * 30)
        if not items:
            lines.append("  今日无新增")
        else:
            for idx, r in enumerate(items, 1):
                mark = "  【资源/破解】" if is_warez(r) else ""
                lines.append(f"{idx}. {r['full_name']}  ★{fmt_star(r.get('stargazers_count'))}{mark}")
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
    total = sum(len(i) for _, i, _n in sections)
    h = [
        '<div style="font-family:-apple-system,Segoe UI,Helvetica,Arial,sans-serif;'
        'font-size:14px;line-height:1.6;color:#24292f;max-width:760px">',
        f'<h2 style="margin:0 0 4px">{esc(title)}</h2>',
        f'<p style="margin:0 0 18px;color:#57606a">生成时间 {esc(generated_at)} · '
        f'共 <b>{total}</b> 个新项目</p>',
    ]
    if failed:
        h.append(
            '<p style="background:#fff8c5;border-left:3px solid #d4a72c;padding:8px 12px;'
            'margin:0 0 16px;color:#7d4e00">以下分类本次拉取失败，未包含结果：'
            + esc("、".join(failed))
            + "</p>"
        )
    for task_name, items, note in sections:
        h.append(
            f'<h3 style="margin:22px 0 8px;font-size:15px;border-bottom:1px solid #d0d7de;'
            f'padding-bottom:5px">{esc(task_name)}'
            + (f'<span style="font-weight:400;color:#8c959f;font-size:12px"> · {esc(note)}</span>' if note else "")
            + "</h3>"
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
    """GitHub Actions 多行 output，用随机分隔符避免内容里出现 EOF 撞车。

    写 output 失败不该让整个 job 崩 —— 邮件已经能发了，output 只是给后续
    step 传值用的。这里吞掉异常并返回 False。
    """
    gh_out = os.getenv("GITHUB_OUTPUT")
    if not gh_out:
        return False
    delim = "EOF_" + uuid.uuid4().hex
    try:
        with open(gh_out, "a", encoding="utf-8") as f:
            f.write(f"{name}<<{delim}\n{value}\n{delim}\n")
        return True
    except OSError as e:
        log(f"[warn] 写 GITHUB_OUTPUT 失败 {name}: {e}")
        return False


def main():
    log(f"monitor.py 版本 {VERSION} · Python {sys.version.split()[0]}")
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

        query, lo, hi = build_query_ex(raw or primary)
        log(f"[{task['name']}] query({source}) = {query}")

        items = fetch_repos(query)
        fetch_failed = items is None
        if fetch_failed:
            items = []

        # 命中太少就用短词兜底再拉一次，避免整类空白。
        # 注意：用户配了 SEARCH_QUERY_* 时同样要走这里。之前写了 `and not raw`，
        # 结果「只要用户填了变量，兜底永远不触发」—— 而用户填的恰恰是
        # 5~6 个裸关键词 AND 在一起的长 query，GitHub 对裸词是 AND 语义，
        # 实测 total=0 或 1，于是九个分类全空，用户看到的就是「什么都没抓到」。
        if not fetch_failed and len(items) < MIN_HITS:
            fb, flo, fhi = build_query_ex(fallback)
            log(f"  主 query 仅 {len(items)} 条，用兜底 query 重试：{fb}")
            more = fetch_repos(fb)
            if more:
                merged = {str(r.get("id")): r for r in items}
                merged.update({str(r.get("id")): r for r in more})
                items = list(merged.values())
                # 兜底用的通常是默认区间，本地复筛取更宽松的那个
                lo, hi = min(lo, flo), max(hi, fhi)
                log(f"  合并后 {len(items)} 条")

        filtered = [r for r in items if not is_blacklisted(r) and star_in_range(r, lo, hi)]

        hist_path = HISTORY_DIR / task["file"]
        seen = load_seen(hist_path)

        new_items = []
        for r in filtered:
            rid = str(r.get("id"))
            if rid in seen or rid in claimed:
                continue
            new_items.append(r)
            claimed.add(rid)

        new_items.sort(key=lambda r: r.get("created_at") or "", reverse=True)
        dropped = max(0, len(new_items) - PER_TASK_LIMIT)
        new_items = new_items[:PER_TASK_LIMIT]

        # 只把「这次真的写进邮件」的记进历史。
        # 之前是先 seen.update(全部 filtered) 再截断到 PER_TASK_LIMIT，
        # 结果第 16 条以后被标记为已见却没进过邮件 —— 永远不会再报，
        # 等于被静默吞掉。现在没报出去的留在池子里，下次继续报，
        # 邮件里显示的创建时间是真实时间，不会把老仓库说成新的。
        seen.update(str(r.get("id")) for r in new_items if r.get("id"))
        try:
            save_seen(hist_path, seen)
        except OSError as e:
            log(f"  [warn] 写历史失败 {hist_path}: {e}")

        if fetch_failed:
            failed.append(task["name"])

        stats.append((task["name"], len(items), len(filtered), len(new_items)))
        # note 只放客观事实：抓取失败 / 还有多少条积压下次报。
        # 不再显示全局 star 区间 —— 每个分类的区间可能不同（用户自己写了 stars:），
        # 写死一个区间会误导。
        note = []
        if fetch_failed:
            note.append("本次拉取失败")
        if dropped:
            note.append(f"另有 {dropped} 条未列出，明后天继续")
        sections.append((task["name"], new_items, "，".join(note)))
        log(f"  拉到 {len(items)} / 过滤后 {len(filtered)} / 新增 {len(new_items)}"
            + (f" / 积压 {dropped}" if dropped else "")
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
    # 全类失败时邮件照发（人得知道监控瞎了），但把状态抛给 workflow 让 job 变红
    all_failed = bool(failed) and len(failed) == len(WATCH_TASKS)
    write_output("fetch_all_failed", "true" if all_failed else "false")

    print("===== 邮件正文（纯文本预览）=====")
    print(text_body)
    if not ok:
        log(f"\n[local] GITHUB_OUTPUT 未设置，结果已写入 {OUT_DIR.resolve()}")

    total_new = sum(c for _, _, _, c in stats)
    log(f"\n完成：{total_new} 个新项目" + (f"，失败分类 {len(failed)} 个" if failed else ""))
    # 注意：全部类别失败也不在这里返回非 0 —— 否则后面的发信步骤会被跳过，
    # 用户就连「监控已经瞎了」这条告警邮件都收不到。改由 workflow 末尾的
    # 「监控健康检查」步骤读 fetch_all_failed 再让 job 变红。
    return 0


if __name__ == "__main__":
    sys.exit(main())