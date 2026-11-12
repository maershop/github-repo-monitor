import os
import requests

GH_TOKEN = os.getenv("GH_TOKEN")
FETCH_COUNT = 30

# 任务配置：从环境变量读取，只配置名称、环境变量名、历史文件名
WATCH_TASKS = [
    {
        "name": "一、LLM / Agent 方向",
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
    }
]

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
        old_ids = load_old_ids(task["history_file"])
        new_items = [r for r in items if str(r["id"]) not in old_ids]
        save_ids(task["history_file"], items)
        all_lines.extend(render_section(task["name"], new_items))

    content = "\n".join(all_lines)
    print("===== 邮件正文 =====")
    print(content)

    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
        f.write(f"mail_body<<EOF\n{content}\nEOF\n")

if __name__ == "__main__":
    main()
