# -*- coding: utf-8 -*-
"""离线自测：不联网，用假数据跑 monitor.py 的分支。"""
import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent
tmp = Path(tempfile.mkdtemp(prefix="ghmon-"))
os.chdir(tmp)
os.environ.pop("GITHUB_OUTPUT", None)
os.environ["GH_TOKEN"] = "dummy"
os.environ["HISTORY_DIR"] = "history"
os.environ["OUT_DIR"] = "out"

spec = importlib.util.spec_from_file_location("monitor", ROOT / "monitor.py")
m = importlib.util.module_from_spec(spec)
spec.loader.exec_module(m)

FAKE = [
    {"id": 1, "full_name": "openai/agents-sdk", "stargazers_count": 1200,
     "language": "Python", "description": "A framework for building local LLM agents",
     "html_url": "https://github.com/openai/agents-sdk", "created_at": "2026-11-20T10:00:00Z"},
    {"id": 2, "full_name": "foo/短剧解析神器", "stargazers_count": 900,
     "language": "Python", "description": "红果短剧 vip解析",
     "html_url": "https://github.com/foo/x", "created_at": "2026-11-19T10:00:00Z"},
    {"id": 3, "full_name": "bar/tiny", "stargazers_count": 5,
     "language": "Python", "description": "too few stars",
     "html_url": "https://github.com/bar/tiny", "created_at": "2026-11-18T10:00:00Z"},
    {"id": 4, "full_name": "baz/huge", "stargazers_count": 99999,
     "language": "Python", "description": "too many stars",
     "html_url": "https://github.com/baz/huge", "created_at": "2026-11-18T10:00:00Z"},
    {"id": 5, "full_name": "acme/textpipe", "stargazers_count": 320,
     "language": "Python", "description": "PDF scrape to markdown pipeline <b>x</b>",
     "html_url": "https://github.com/acme/textpipe", "created_at": "2026-11-21T10:00:00Z"},
]

failures = []


def check(name, cond, extra=""):
    print(("PASS  " if cond else "FAIL  ") + name + (f"  {extra}" if extra and not cond else ""))
    if not cond:
        failures.append(name)


REAL_FETCH = m.fetch_repos   # 保存真函数，后面测 per_page 要用

# --- query 补全 ---------------------------------------------------------
q = m.build_query("agent framework language:python stars:>30")
check("build_query 追加 created:", "created:>" in q, q)
check("build_query 追加 fork:false", "fork:false" in q, q)
q2 = m.build_query("x created:>2025-01-01 fork:false")
check("build_query 不重复追加", q2.count("created:") == 1 and q2.count("fork:") == 1, q2)
q3 = m.build_query("x pushed:>2025-01-01")
# pushed: 只是「最近提交过」，会把老仓库当新项目报。所以 created: 照样要补，
# 两个条件是交集：既要 created 在窗口内，又要 push 在用户给的范围内。
check("build_query 写了 pushed: 也补 created:", "created:>" in q3 and "pushed:>" in q3, q3)
check("build_query 空串返回空", m.build_query("") == "", m.build_query(""))

# --- star 限定符归一化 --------------------------------------------------
# 用户配的 stars:>30 stars:<2500 正好踩中「两个 stars: 只认后一个」的坑，
# 必须被合并成单个区间。
qs = m.build_query("agent framework local llm language:python stars:>30 stars:<2500")
check("两个 stars: 合并成一个", qs.count("stars:") == 1, qs)
check(">30 变下界 31", "stars:31..2499" in qs, qs)
check("stars:<10 不被 MIN_STAR 顶掉", "stars:0..9" in m.build_query("x stars:<10"),
      m.build_query("x stars:<10"))
check("stars:>=100 解析正确", "stars:100..2500" in m.build_query("x stars:>=100"),
      m.build_query("x stars:>=100"))
check("stars:100..500 原样保留", "stars:100..500" in m.build_query("x stars:100..500"),
      m.build_query("x stars:100..500"))
check("无法解析的 stars: 退回默认", "stars:30..2500" in m.build_query("x stars:abc"),
      m.build_query("x stars:abc"))
qf = m.build_query("x -fork:true")
check("否定式 -fork:true 被统一成 fork:false", qf.count("fork:") == 1 and "fork:false" in qf, qf)

# --- build_query_ex 必须把生效区间带出来，本地复筛才对得上 ----------------
qex, lo, hi = m.build_query_ex("x stars:100..500")
check("build_query_ex 返回区间", (lo, hi) == (100, 500), (qex, lo, hi))
check("本地复筛用生效区间",
      m.star_in_range({"stargazers_count": 500}, 0, 9) is False
      and m.star_in_range({"stargazers_count": 5}, 0, 9) is True)
check("默认 query 都带兜底", all(isinstance(v, tuple) and len(v) == 2 for v in m.QUERY_DEFAULTS.values()))

# --- 过滤 --------------------------------------------------------------
check("正常仓库通过黑名单", not m.is_blacklisted(FAKE[0]))
check("中文黑名单命中", m.is_blacklisted(FAKE[1]))
# keygen 属于「资源类」关键词，默认保留，开启 BLOCK_WAREZ 后才拦
m.BLOCK_WAREZ = True
check("topics 也参与判断",
      m.is_blacklisted({"full_name": "a/b", "description": "", "topics": ["keygen"]}))
check("demo 类仓库被拦",
      m.is_blacklisted({"full_name": "u/hello-world", "description": ""}))
check("scrape 不误杀（第二类本身就是采集管线）",
      not m.is_blacklisted({"full_name": "a/pdf-scraper", "description": "scrape pdf"}))
check("star 区间内", m.star_in_range(FAKE[0]))
check("star 过少", not m.star_in_range(FAKE[2]))
check("star 过多", not m.star_in_range(FAKE[3]))
check("star 缺失不崩", not m.star_in_range({"stargazers_count": None}))

# --- 第一轮：全部是新增 ------------------------------------------------
m.fetch_repos = lambda q: list(FAKE)
rc = m.main()
check("main 返回 0", rc == 0, rc)

hist = Path("history/last_agent.txt")
check("历史文件已写入", hist.exists())
ids = set(hist.read_text(encoding="utf-8").split())
check("历史只记录通过过滤的 id", ids == {"1", "5"}, ids)

text = Path("out/mail.txt").read_text(encoding="utf-8")
html = Path("out/mail.html").read_text(encoding="utf-8")
check("正文含新仓库", "openai/agents-sdk" in text)
check("正文不含黑名单仓库", "短剧" not in text)
check("html 转义 <b>", "&lt;b&gt;x&lt;/b&gt;" in html, [l for l in html.splitlines() if "textpipe" in l][:1])
check("html 是合法片段", html.count('<div style="font-family') == 1 and html.rstrip().endswith("</div>"))
check("跨分类去重：同一仓库只出现一次",
      text.count("链接：https://github.com/openai/agents-sdk") == 1,
      text.count("链接：https://github.com/openai/agents-sdk"))

# --- 第二轮：同一批数据，应为 0 新增（去重生效）-------------------------
import io
from contextlib import redirect_stdout

buf = io.StringIO()
with redirect_stdout(buf):
    m.main()
out2 = buf.getvalue()
check("第二轮无新增", "今日无新增" in out2)

# --- 第三轮：历史不被清空（模拟 API 返回空）----------------------------
m.fetch_repos = lambda q: []
buf = io.StringIO()
with redirect_stdout(buf):
    m.main()
ids_after = set(hist.read_text(encoding="utf-8").split())
check("API 空返回不清空历史", ids_after == {"1", "5"}, ids_after)

# --- GITHUB_OUTPUT 多行分隔符 -----------------------------------------
m.fetch_repos = lambda q: []
gho = Path("gh_output.txt")
gho.write_text("")
os.environ["GITHUB_OUTPUT"] = str(gho.resolve())
buf = io.StringIO()
with redirect_stdout(buf):
    m.main()
raw = gho.read_text(encoding="utf-8")
check("GITHUB_OUTPUT 写入 mail_body", "mail_body<<" in raw)
check("GITHUB_OUTPUT 用随机分隔符", "EOF_" in raw and "\nEOF\n" not in raw)
check("GITHUB_OUTPUT 含 has_new", "has_new<<" in raw)

# --- 限流分页参数边界 --------------------------------------------------
captured = {}


class FakeResp:
    """模拟 urlopen 的返回对象（上下文管理器 + read）。"""

    def __init__(self, payload=b'{"items": []}'):
        self._payload = payload

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def fake_urlopen(req, timeout=None):
    from urllib.parse import parse_qs, urlparse
    qs = parse_qs(urlparse(req.full_url).query)
    for k, v in qs.items():
        captured[k] = v[0]
    return FakeResp()


m.urlopen = fake_urlopen
os.environ["FETCH_COUNT"] = "500"
m.FETCH_COUNT = 500
real = REAL_FETCH("x")
check("per_page 被钳到 100", captured.get("per_page") == "100", captured)
check("成功空结果是 list 而不是 None", real == [], real)
check("query 用 URL 参数传而不是拼在路径里", captured.get("q") == "x", captured)


# 失败路径返回 None（模拟 401）
def bad_urlopen(req, timeout=None):
    from urllib.error import HTTPError
    raise HTTPError(req.full_url, 401, "Bad credentials", {}, None)


m.urlopen = bad_urlopen
res = REAL_FETCH("x")
check("401 返回 None 表示失败", res is None, res)


# 配置错误不重试：401/422 只打一次日志
_calls = {"n": 0}


def counting_urlopen(req, timeout=None):
    from urllib.error import HTTPError
    _calls["n"] += 1
    raise HTTPError(req.full_url, 422, "Unprocessable", {}, None)


m.urlopen = counting_urlopen
REAL_FETCH("x")
check("422 配置错误不重试（只请求 1 次）", _calls["n"] == 1, _calls)

print()
# --- 盗版分发识别 -------------------------------------------------------
# 这些名字来自真实 API 返回，第九类 PDF/OCR 查询会把它们全捞上来。
_WAREZ = [
    "tiansokolov/pdfelement-win",
    "ArunHall08/abbyy-finereader-pdf-win",
    "orakio1975/acrobat-pro-pdf-editor",
    "Blaqsoft/acrobat-pro-fajalor-pdf-workspace",
    "Vig-13/Acrobat-Pro-For-Windows",
    "ALIASLUBUSU/abby-pdf-transformer",
    "Dikoks/acrobat-pro-master-pdf-2026",
]
# 默认（BLOCK_WAREZ=0）这类仓库要保留 —— 用户要的就是这些资源库
m.BLOCK_WAREZ = False
check("默认保留破解/资源分发库",
      not any(m.is_blacklisted({"full_name": n, "description": "", "topics": []}) for n in _WAREZ),
      [n for n in _WAREZ if m.is_blacklisted({"full_name": n, "description": "", "topics": []})])

# 开启开关后才拦
m.BLOCK_WAREZ = True
check("BLOCK_WAREZ=1 时盗版分发仓库被拦下",
      all(m.is_blacklisted({"full_name": n, "description": "", "topics": []}) for n in _WAREZ),
      [n for n in _WAREZ if not m.is_blacklisted({"full_name": n, "description": "", "topics": []})])

# 正常开源项目不能被商品名误杀
_CLEAN = [
    "bulkgrid/bulkparse", "NPCmillionaire/unredact.tools",
    "apisitwattanamongkol/pdf-ocr-desktop", "langgenius/dify",
    "tesseract-ocr/tesseract", "microsoft/markitdown",
]
# 即使开了开关，正常开源项目也不能被商品名误杀
m.BLOCK_WAREZ = True
check("正常项目不被盗版规则误杀",
      not any(m.is_blacklisted({"full_name": n, "description": "", "topics": []}) for n in _CLEAN),
      [n for n in _CLEAN if m.is_blacklisted({"full_name": n, "description": "", "topics": []})])
m.BLOCK_WAREZ = False

print("=" * 40)
if failures:
    print(f"{len(failures)} 个用例失败: {failures}")
    sys.exit(1)
print("全部用例通过")
shutil.rmtree(tmp, ignore_errors=True)