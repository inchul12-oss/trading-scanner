"""
스캐너1-미 결과(scanner1_result.json)를 텔레그램으로 전송한다.
카카오톡에서 텔레그램으로 전환(2026-08-31) - OAuth 갱신 불필요, 봇 토큰 하나로 끝.
필요한 환경변수: TELEGRAM_BOT_TOKEN
"""
import json
import os
import urllib.parse
import urllib.request
from datetime import datetime, timezone, timedelta

BOT_TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
CHAT_ID = "-5569815780"  # 인철님+친구 텔레그램 그룹("트레이딩스캐너")

KST = timezone(timedelta(hours=9))


def to_kst_hhmm(updated_at_utc):
    """UTC ISO 문자열을 한국시간(KST) HH:MM으로 변환한다.
    9/3 추가: 인철님이 한국에서 보는데 메시지엔 UTC로 찍혀서 매번 +9시간 암산해야 했던 문제 수정
    (국장/카카오는 원래부터 KST로 뜨고 있었음). 파싱 실패시 예전처럼 UTC 슬라이스로 안전하게 대체."""
    try:
        dt = datetime.fromisoformat(updated_at_utc.replace("Z", "+00:00"))
        return dt.astimezone(KST).strftime("%H:%M")
    except (ValueError, AttributeError):
        return updated_at_utc[11:16] if len(updated_at_utc) >= 16 else updated_at_utc


def build_message(result):
    matches = result.get("matches", [])
    updated = result.get("updated_at_utc", "")
    time_label = to_kst_hhmm(updated)
    lines = [f"탐지신호-미 결과 ({time_label} KST)"]
    if not matches:
        lines.append("조건에 맞는 종목 없음")
    else:
        lines.append(f"{len(matches)}건 매칭")
        for m in matches:
            tag = "" if m.get("is_premarket_data") else " (정규장가)"
            gap_tag = " ⚠️설거지리스크(갭+100%↑)" if m.get("extreme_gap") else ""
            lines.append(f"{m['symbol']} ${m['price']:.2f} {m['change_pct']:+.1f}%{tag}{gap_tag}")
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
