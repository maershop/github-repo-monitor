import requests
import os

# ========= 修改搜索条件 =========
query = "topic:llm-agent stars:>100 created:>2026-01-01"
# =================================
gh_token = os.getenv("GH_TOKEN")
headers = {"Authorization": f"token:{gh_token}"}
url = f"https://api.github.com/search/repositories?q={query}&sort=created&order=desc&per_page=30"
resp = requests.get(url, headers=headers)
data = resp.json()
items = data.get("items", [])

old_ids = set()
try:
    with open("last.txt", "r", encoding="utf-8") as f:
        old_ids = set(f.read().splitlines())
except FileNotFoundError:
    pass

new_repos = [item for item in items if str(item["id"]) not in old_ids]

# 保存本次ID
with open("last.txt", "w", encoding="utf-8") as f:
    f.write("\n".join([str(x["id"]) for x in items]))

if new_repos:
    body_text = "# 📢 GitHub新项目日报\n\n"
    for repo in new_repos:
        star = repo["stargazers_count"]
        name = repo["full_name"]
        link = repo["html_url"]
        desc = repo["description"] or "无简介"
        body_text += f"- [{name}]({link}) ⭐{star}\n> {desc}\n\n"
else:
    body_text = "✅ 今日没有匹配条件的新项目"

with open("mail_body.txt", "w", encoding="utf-8") as f:
    f.write(body_text)
