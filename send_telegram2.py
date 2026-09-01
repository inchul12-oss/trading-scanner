"""
스캐너2-미 진입신호(scanner2_result.json)를 텔레그램으로 전송한다.
진입신호가 하나도 없으면 전송하지 않는다(스팸 방지).
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
    entries = result.get("entries", [])
    updated = result.get("updated_at_utc", "")
    time_label = updated[11:16] if len(updated) >= 16 else updated
    lines = [f"진입신호-미 ({time_label} UTC)"]
    for e in entries:
        vol = e.get("volume_confirmed")
        if vol is True:
            tag = "거래량확인"
        elif vol is False:
            tag = "거래량부족"
        else:
            tag = "거래량미확인(가격조건만)"
        price = e.get("price")
        lines.append(f"{e['symbol']} {price:.2f} - 진입검토 ({tag})")
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
    with open("scanner2_result.json", encoding="utf-8") as f:
        result = json.load(f)

    entries = result.get("entries", [])
    if not entries:
        print("진입신호 없음 - 텔레그램 전송 생략(스팸 방지)")
        return

    message = build_message(result)
    print(message)
    send_telegram_message(message)


if __name__ == "__main__":
    main()
