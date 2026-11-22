import os
import requests

GH_TOKEN = os.getenv("GH_TOKEN")
FETCH_COUNT = 30
# star 区间统一阈值
MIN_STAR = 30
MAX_STAR = 2500

# 黑名单：简介、仓库名命中下面任意词直接跳过（小写匹配）
BLACKLIST_KEYWORDS = {
    "短剧", "红果", "解析", "爬虫", "抓取", "破解", "vip解析",
    "影视解析", "视频抓取", "douyin抓取", "tiktok爬虫"
}

# 任务配置：新增赛道，对齐你要的方向
WATCH_TASKS = [
    {
        "name": "一、LLM / Agent 智能体方向",
        "env_key": "SEARCH_QUERY_AGENT",
        "history_file": "last_agent.txt"
    },
    {
        "name": "二、资料采集清洗整理（网页/PDF文档RAG管线）",
        "env_key": "SEARCH_QUERY_DOC_PIPELINE",
        "history_file": "last_doc_pipeline.txt"
    },
    {
        "name": "三、Excel/CSV表格、位次数据清洗",
        "env_key": "SEARCH_QUERY_TABLE",
        "history_file": "last_table.txt"
    },
    {
        "name": "四、RAG知识库构建、文档入库",
        "env_key": "SEARCH_QUERY_RAG_KB",
        "history_file": "last_rag_kb.txt"
    },
    {
        "name": "五、短视频/直播文案生成Agent",
        "env_key": "SEARCH_QUERY_SCRIPT",
        "history_file": "last_script_agent.txt"
    },
    {
        "name": "六、网站、前端、CMS、设计工具",
        "env_key": "SEARCH_QUERY_WEB_DESIGN",
        "history_file": "last_web_design.txt"
    },
    {
        "name": "七、开源教育平台、志愿/题库系统",
        "env_key": "SEARCH_QUERY_EDUCATION",
        "history_file": "last_education.txt"
    },
    {
        "name": "八、新媒体直播工具（提词器、字幕、直播看板）",
        "env_key": "SEARCH_QUERY_LIVE_TOOLS",
        "history_file": "last_live_tools.txt"
    },
    {
        "name": "九、自动化办公、PDF/OCR/RPA",
        "env_key": "SEARCH_QUERY_OFFICE_AUTO",
        "history_file": "last_office_auto.txt"
    }
]


def is_blacklisted(repo) -> bool:
    """黑名单过滤，仓库全名 + description小写匹配"""
    text = f"{repo['full_name']} {repo.get('description','')}".lower()
    for kw in BLACKLIST_KEYWORDS:
        if kw in text:
            return True
    return False


def star_in_range(repo) -> bool:
    st = repo["stargazers_count"]
    return MIN_STAR <= st <= MAX_STAR


def fetch_repos(query):
    headers = {
        "Authorization": f"token {GH_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    url = (
        f"https://api.github.com/search/repositories"
        f"?q={query}&sort=created&order=desc&per_page={FETCH_COUNT}"
    )
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    return resp.json().get("items", [])


def load_old_ids(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            return {line.strip() for line in f if line.strip()}
    except FileNotFoundError:
        return set()


def save_ids(path, items):
    with open(path, "w", encoding="utf-8") as f:
        for repo in items:
            f.write(f"{repo['id']}\n")


def render_section(task_name, new_items):
    lines = [task_name, "-" * 30, ""]
    if not new_items:
        lines.append("  今日无新增")
    else:
        for idx, repo in enumerate(new_items, 1):
            name = repo["full_name"]
            star = repo["stargazers_count"]
            lang = repo.get("language") or "未标注"
            desc = repo.get("description") or "无简介"
            link = repo["html_url"]
            lines.append(f"{idx}. {name}  ⭐{star}")
            lines.append(f"   语言：{lang}")
            lines.append(f"   简介：{desc}")
            lines.append(f"   链接：{link}")
            lines.append("")
    lines.append("")
    return lines


def main():
    if not GH_TOKEN:
        print("错误：GH_TOKEN 未配置")
        exit(1)

    all_lines = ["GitHub新项目监控日报", "=" * 40, ""]

    for task in WATCH_TASKS:
        query = os.getenv(task["env_key"])
        if not query:
            all_lines.append(f'{task["name"]}：未配置搜索关键词\n')
            continue
        items = fetch_repos(query)
        # 两层过滤：黑名单 + star区间
        filtered_items = [
            r for r in items
            if (not is_blacklisted(r)) and star_in_range(r)
        ]
        old_ids = load_old_ids(task["history_file"])
        new_items = [r for r in filtered_items if str(r["id"]) not in old_ids]
        save_ids(task["history_file"], filtered_items)
        all_lines.extend(render_section(task["name"], new_items))

    content = "\n".join(all_lines)
    print("===== 邮件正文 =====")
    print(content)

    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
        f.write(f"mail_body<<EOF\n{content}\nEOF\n")


if __name__ == "__main__":
    main()
