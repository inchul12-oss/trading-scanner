"""
스캐너3-미 매도/손절 신호(new_exits) + 1차 익절 신호(partial_profit_alerts)를
텔레그램으로 전송한다.
둘 다 없으면 전송하지 않는다(스팸 방지).
매도신호 중 긴급(직선급락) 청산이 하나라도 있으면 그 섹션 헤더에 🚨 태그를 붙인다.
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
    exits = result.get("new_exits", [])
    partials = result.get("partial_profit_alerts", [])
    updated = result.get("updated_at_utc", "")
    time_label = updated[11:16] if len(updated) >= 16 else updated
    any_urgent = any(e.get("urgent") for e in exits)

    lines = []

    if exits:
        header = "🚨 청산신호-미 긴급 매도신호" if any_urgent else "청산신호-미 매도/손절 신호"
        lines.append(f"{header} ({time_label} UTC)")
        for e in exits:
            tag = "🚨" if e.get("urgent") else "-"
            reasons = ", ".join(e.get("exit_reasons", []))
            pnl = e.get("pnl_pct", 0.0) * 100
            lines.append(f"{tag} {e['symbol']} {e['exit_price']:.2f} ({pnl:+.1f}%) - {reasons}")

    if partials:
        if lines:
            lines.append("")
        lines.append(f"💰 청산신호-미 1차 익절 고려 ({time_label} UTC)")
        for p in partials:
            pnl = p.get("pnl_pct", 0.0) * 100
            lines.append(f"💰 {p['symbol']} {p['price']:.2f} ({pnl:+.1f}%) - 목표(+10%) 도달, 절반 익절 고려(나머지는 트레일링 유지)")

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
    with open("scanner3_result.json", encoding="utf-8") as f:
        result = json.load(f)

    exits = result.get("new_exits", [])
    partials = result.get("partial_profit_alerts", [])
    if not exits and not partials:
        print("신규 청산/1차익절 신호 없음 - 텔레그램 전송 생략(스팸 방지)")
        return

    message = build_message(result)
    print(message)
    send_telegram_message(message)


if __name__ == "__main__":
    main()
