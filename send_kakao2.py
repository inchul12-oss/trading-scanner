"""
스캐너2-미 진입신호(scanner2_result.json)를 카카오톡 '나에게 보내기'로 전송한다.
진입신호가 하나도 없으면 전송하지 않는다(스팸 방지).
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
    entries = result.get("entries", [])
    updated = result.get("updated_at_utc", "")
    time_label = updated[11:16] if len(updated) >= 16 else updated
    lines = [f"스캐너2-미 진입신호 ({time_label} UTC)"]
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
    with open("scanner2_result.json", encoding="utf-8") as f:
        result = json.load(f)

    entries = result.get("entries", [])
    if not entries:
        print("진입신호 없음 - 카카오 전송 생략(스팸 방지)")
        return

    access_token = refresh_access_token()
    message = build_message(result)
    print(message)
    send_kakao_message(access_token, message)


if __name__ == "__main__":
    main()

