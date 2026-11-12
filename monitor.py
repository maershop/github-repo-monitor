import requests
import os
import smtplib
from email.mime.text import MIMEText

# ========== 配置区 ==========
SEARCH_QUERY = "llm agent stars:>50 created:>2026-01-01"
FETCH_COUNT = 30

SMTP_SERVER = "smtp.qq.com"
SMTP_PORT = 465
SMTP_USER = os.getenv("EMAIL_USER")
SMTP_PASS = os.getenv("EMAIL_PASS")
MAIL_TO = os.getenv("EMAIL_TO")
MAIL_SUBJECT = "GitHub新项目监控日报"
# ============================

def send_email(text_content):
    msg = MIMEText(text_content, "plain", "utf-8")
    msg["Subject"] = MAIL_SUBJECT
    msg["From"] = SMTP_USER
    msg["To"] = MAIL_TO
    with smtplib.SMTP_SSL(SMTP_SERVER, SMTP_PORT) as server:
        server.login(SMTP_USER, SMTP_PASS)
        server.send_message(msg)
    print("✅ 邮件发送成功")

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
        send_email("今日无符合条件的新项目")
        return

    old_ids = set()
    try:
        with open("last.txt", "r", encoding="utf-8") as f:
            old_ids = set(line.strip() for line in f)
    except FileNotFoundError:
        print("【提示】首次运行，创建last.txt，本次不推送新增")

    new_items = [i for i in items if str(i["id"]) not in old_ids]

    with open("last.txt", "w", encoding="utf-8") as f:
        for i in items:
            f.write(f"{i['id']}\n")

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

    mail_content = "\n".join(lines)
    print("===== 即将发送的邮件正文 =====")
    print(mail_content)
    print("===== 正文结束 =====")
    print(f"执行完成，新增项目数: {len(new_items)}")

    send_email(mail_content)

if __name__ == "__main__":
    main()
