"""
스캐너1-미 결과(scanner1_result.json)를 텔레그램으로 전송한다.
카카오톡에서 텔레그램으로 전환(2026-08-31) - OAuth 갱신 불필요, 봇 토큰 하나로 끝.
필요한 환경변수: TELEGRAM_BOT_TOKEN
"""
import json
import os
import urllib.parse
import urllib.request

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = "-5569815780"  # 인철님+친구 텔레그램 그룹("트레이딩스캐너")


def build_message(result):
    matches = result.get("matches", [])
    updated = result.get("updated_at_utc", "")
    time_label = updated[11:16] if len(updated) >= 16 else updated
    lines = [f"스캐너1-미 결과 ({time_label} UTC)"]
    if not matches:
        lines.append("조건에 맞는 종목 없음")
    else:
        lines.append(f"{len(matches)}건 매칭")
        for m in matches:
            tag = "" if m.get("is_premarket_data") else " (정규장가)"
            lines.append(f"{m['symbol']} {m['change_pct']:+.1f}%{tag}")
    return "\n".join(lines)


def send_telegram_message(text):
    data = urllib.parse.urlencode({
        "chat_id": CHAT_ID,
        "text": text,
    }).encode()
    req = urllib.request.Request(
        f"https://api.telegram.org/bot{BOT_TOKEN}/sendMessage",
        data=data,
    )
    with urllib.request.urlopen(req) as res:
        print(res.read().decode())


def main():
    with open("scanner1_result.json", encoding="utf-8") as f:
        result = json.load(f)

    message = build_message(result)
    print(message)
    send_telegram_message(message)


if __name__ == "__main__":
    main()

