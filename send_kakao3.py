"""
스캐너3-미 매도/손절 신호(new_exits) + 1차 익절 신호(partial_profit_alerts)를
카카오톡 '나에게 보내기'로 전송한다.
둘 다 없으면 전송하지 않는다(스팸 방지).
매도신호 중 긴급(직선급락) 청산이 하나라도 있으면 그 섹션 헤더에 🚨 태그를 붙인다.
필요한 환경변수: KAKAO_REST_API_KEY, KAKAO_CLIENT_SECRET, KAKAO_REFRESH_TOKEN
"""
import json
import os
import urllib.parse
import urllib.request

REST_API_KEY = os.environ["KAKAO_REST_API_KEY"]
CLIENT_SECRET = os.environ["KAKAO_CLIENT_SECRET"]
REFRESH_TOKEN = os.environ["KAKAO_REFRESH_TOKEN"]


def refresh_access_token():
    data = urllib.parse.urlencode({
        "grant_type": "refresh_token",
        "client_id": REST_API_KEY,
        "client_secret": CLIENT_SECRET,
        "refresh_token": REFRESH_TOKEN,
    }).encode()
    req = urllib.request.Request(
        "https://kauth.kakao.com/oauth/token",
        data=data,
        headers={"Content-Type": "application/x-www-form-urlencoded;charset=utf-8"},
    )
    with urllib.request.urlopen(req) as res:
        body = json.loads(res.read().decode())
    return body["access_token"]


def build_message(result):
    exits = result.get("new_exits", [])
    partials = result.get("partial_profit_alerts", [])
    updated = result.get("updated_at_utc", "")
    time_label = updated[11:16] if len(updated) >= 16 else updated
    any_urgent = any(e.get("urgent") for e in exits)

    lines = []

    if exits:
        header = "🚨 스캐너3-미 긴급 매도신호" if any_urgent else "스캐너3-미 매도/손절 신호"
        lines.append(f"{header} ({time_label} UTC)")
        for e in exits:
            tag = "🚨" if e.get("urgent") else "-"
            reasons = ", ".join(e.get("exit_reasons", []))
            pnl = e.get("pnl_pct", 0.0) * 100
            lines.append(f"{tag} {e['symbol']} {e['exit_price']:.2f} ({pnl:+.1f}%) - {reasons}")

    if partials:
        if lines:
            lines.append("")
        lines.append(f"💰 스캐너3-미 1차 익절 고려 ({time_label} UTC)")
        for p in partials:
            pnl = p.get("pnl_pct", 0.0) * 100
            lines.append(f"💰 {p['symbol']} {p['price']:.2f} ({pnl:+.1f}%) - 목표(+10%) 도달, 절반 익절 고려(나머지는 트레일링 유지)")

    return "\n".join(lines)


def send_kakao_message(access_token, text):
    template_object = {
        "object_type": "text",
        "text": text,
        "link": {
            "web_url": "https://github.com/inchul12-oss/trading-scanner",
            "mobile_web_url": "https://github.com/inchul12-oss/trading-scanner",
        },
    }
    data = urllib.parse.urlencode({"template_object": json.dumps(template_object)}).encode()
    req = urllib.request.Request(
        "https://kapi.kakao.com/v2/api/talk/memo/default/send",
        data=data,
        headers={
            "Authorization": f"Bearer {access_token}",
            "Content-Type": "application/x-www-form-urlencoded;charset=utf-8",
        },
    )
    with urllib.request.urlopen(req) as res:
        print(res.read().decode())


def main():
    with open("scanner3_result.json", encoding="utf-8") as f:
        result = json.load(f)

    exits = result.get("new_exits", [])
    partials = result.get("partial_profit_alerts", [])
    if not exits and not partials:
        print("신규 청산/1차익절 신호 없음 - 카카오 전송 생략(스팸 방지)")
        return

    access_token = refresh_access_token()
    message = build_message(result)
    print(message)
    send_kakao_message(access_token, message)


if __name__ == "__main__":
    main()
