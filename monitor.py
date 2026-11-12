import requests
import os

# ========== 配置区 ==========
SEARCH_QUERY = "llm agent stars:>50 created:>2026-01-01"
FETCH_COUNT = 30
# ============================

def main():
    gh_token = os.getenv("GH_TOKEN")
    if not gh_token:
        print("错误：GH_TOKEN 未配置")
        exit(1)

    headers = {
        "Authorization": f"token {gh_token}",
        "Accept": "application/vnd.github.v3+json"
    }
    url = f"https://api.github.com/search/repositories?q={SEARCH_QUERY}&sort=created&order=desc&per_page={FETCH_COUNT}"

    try:
        resp = requests.get(url, headers=headers, timeout=30)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        print(f"API请求失败: {e}")
        exit(1)

    items = data.get("items", [])
    if not items:
        with open("mail_body.txt", "w", encoding="utf-8") as f:
            f.write("今日无符合条件的新项目")
        return

    # 读取历史记录
    old_ids = set()
    try:
        with open("last.txt", "r", encoding="utf-8") as f:
            old_ids = set(line.strip() for line in f)
    except FileNotFoundError:
        pass

    new_items = [i for i in items if str(i["id"]) not in old_ids]

    # 保存本次ID
    with open("last.txt", "w", encoding="utf-8") as f:
        for i in items:
            f.write(f"{i['id']}\n")

    # 生成纯文本正文
    lines = []
    lines.append("GitHub新项目监控日报")
    lines.append("-" * 30)
    lines.append("")
    if new_items:
        for idx, repo in enumerate(new_items, 1):
            name = repo["full_name"]
            star = repo["stargazers_count"]
            lang = repo["language"] or "未标注"
            desc = repo["description"] or "无简介"
            link = repo["html_url"]
            lines.append(f"{idx}. {name}  星标: {star}")
            lines.append(f"   语言: {lang}")
            lines.append(f"   简介: {desc}")
            lines.append(f"   地址: {link}")
            lines.append("")
    else:
        lines.append("今日没有新增匹配项目")

    with open("mail_body.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(lines))

    print(f"执行完成，新增项目数: {len(new_items)}")

if __name__ == "__main__":
    main()
