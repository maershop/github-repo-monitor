import os
import requests

SEARCH_QUERY = "llm agent stars:>50 created:>2026-01-01"
FETCH_COUNT = 30
GH_TOKEN = os.getenv("GH_TOKEN")

def main():
    headers = {
        "Authorization": f"token {GH_TOKEN}",
        "Accept": "application/vnd.github.v3+json"
    }
    url = (
        f"https://api.github.com/search/repositories"
        f"?q={SEARCH_QUERY}&sort=created&order=desc&per_page={FETCH_COUNT}"
    )
    resp = requests.get(url, headers=headers, timeout=30)
    resp.raise_for_status()
    items = resp.json().get("items", [])

    old_ids = set()
    try:
        with open("last.txt", "r", encoding="utf-8") as f:
            old_ids = {line.strip() for line in f}
    except FileNotFoundError:
        print("首次运行，记录仓库ID，本次标记初始化")

    new_items = [repo for repo in items if str(repo["id"]) not in old_ids]

    # 更新历史记录
    with open("last.txt", "w", encoding="utf-8") as f:
        for repo in items:
            f.write(f"{repo['id']}\n")

    lines = ["GitHub新项目监控日报", "-" * 30, ""]
    if new_items:
        for idx, repo in enumerate(new_items, start=1):
            name = repo["full_name"]
            star = repo["stargazers_count"]
            lang = repo.get("language") or "未标注"
            desc = repo.get("description") or "无简介"
            link = repo["html_url"]
            lines.append(f"{idx}. {name}  ⭐{star}")
            lines.append(f"语言：{lang}")
            lines.append(f"简介：{desc}")
            lines.append(f"链接：{link}")
            lines.append("")
    else:
        lines.append("今日没有新增匹配项目")

    content = "\n".join(lines)
    # 输出到action output，给后面邮件action读取
    with open(os.environ["GITHUB_OUTPUT"], "a", encoding="utf-8") as f:
        f.write(f"mail_body<<EOF\n{content}\nEOF\n")

if __name__ == "__main__":
    main()
