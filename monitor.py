import requests
import os

# ========== 可修改配置区 ==========
# GitHub搜索语法，支持关键词、star数、创建时间、语言、topic等
SEARCH_QUERY = "llm agent stars:>50 created:>2026-01-01"
# 单次抓取仓库数量，最大100
FETCH_COUNT = 30
# ==================================

def main():
    # 读取环境变量中的Token
    gh_token = os.getenv("GH_TOKEN")
    if not gh_token:
        print("错误：未获取到GH_TOKEN，请检查仓库Secrets配置")
        exit(1)

    # 构造请求
    headers = {
        "Authorization": f"token {gh_token}",
        "Accept": "application/vnd.github.v3+json"
    }
    api_url = (
        "https://api.github.com/search/repositories"
        f"?q={SEARCH_QUERY}"
        "&sort=created&order=desc"
        f"&per_page={FETCH_COUNT}"
    )

    # 调用GitHub搜索API
    try:
        resp = requests.get(api_url, headers=headers, timeout=30)
        resp.raise_for_status()
        result = resp.json()
    except Exception as e:
        print(f"API请求失败：{str(e)}")
        exit(1)

    repo_list = result.get("items", [])
    if not repo_list:
        print("未搜索到符合条件的仓库")
        with open("mail_body.txt", "w", encoding="utf-8") as f:
            f.write("今日未搜索到符合条件的新项目。")
        return

    # 读取历史记录，过滤已推送过的仓库
    old_ids = set()
    try:
        with open("last.txt", "r", encoding="utf-8") as f:
            old_ids = set(line.strip() for line in f if line.strip())
    except FileNotFoundError:
        print("首次运行，无历史记录，将记录当前所有仓库")

    new_repos = [repo for repo in repo_list if str(repo["id"]) not in old_ids]

    # 保存本次所有仓库ID，用于下次对比
    with open("last.txt", "w", encoding="utf-8") as f:
        for repo in repo_list:
            f.write(f"{repo['id']}\n")

    # 生成纯文本邮件正文（无Markdown特殊符号，避免格式报错）
    if new_repos:
        content = ["GitHub新项目监控日报", "=" * 40, ""]
        for index, repo in enumerate(new_repos, 1):
            full_name = repo["full_name"]
            star_num = repo["stargazers_count"]
            language = repo["language"] if repo["language"] else "未标注"
            description = repo["description"] if repo["description"] else "无项目简介"
            repo_url = repo["html_url"]

            content.append(f"{index}. {full_name}  ⭐{star_num}")
            content.append(f"   开发语言：{language}")
            content.append(f"   项目简介：{description}")
            content.append(f"   项目地址：{repo_url}")
            content.append("")
    else:
        content = ["今日没有匹配条件的新项目。"]

    # 写入邮件正文文件
    with open("mail_body.txt", "w", encoding="utf-8") as f:
        f.write("\n".join(content))

    print(f"执行完成，本次发现 {len(new_repos)} 个新项目")

if __name__ == "__main__":
    main()
