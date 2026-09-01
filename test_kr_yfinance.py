"""
임시 테스트 스크립트: 야후파이낸스(yfinance)가 한국장 종목(NXT 프리마켓 포함)의
프리마켓 데이터와 1분봉 히스토리, 급등주 스크리너를 실제로 제공하는지 확인용.
확인 끝나면 이 파일과 워크플로우는 삭제할 예정 (일회성 조사 목적).
"""
import yfinance as yf

SYMBOLS = ["005930.KS", "035720.KS"]  # 삼성전자, 카카오

for sym in SYMBOLS:
    print(f"\n=== {sym} ===")
    t = yf.Ticker(sym)
    info = t.get_info()
    fields = [
        "symbol", "shortName", "regularMarketPrice", "regularMarketDayHigh",
        "regularMarketDayLow", "previousClose", "preMarketPrice", "preMarketVolume",
        "preMarketChangePercent", "postMarketPrice", "regularMarketVolume",
        "exchange", "marketState", "currency",
    ]
    for f in fields:
        print(f"  {f}: {info.get(f)}")

    try:
        intraday = t.history(period="1d", interval="1m", prepost=True)
        print(f"  1분봉 행 개수: {len(intraday)}")
        if not intraday.empty:
            print(f"  첫 시각: {intraday.index[0]}")
            print(f"  마지막 시각: {intraday.index[-1]}")
    except Exception as e:
        print(f"  1분봉 에러: {e}")

try:
    res = yf.screen("day_gainers", count=5)
    print("\n=== day_gainers 스크리너 결과 (한국 종목 포함되는지 확인) ===")
    for q in res.get("quotes", [])[:5]:
        print(f"  {q.get('symbol')} - {q.get('exchange')}")
except Exception as e:
    print(f"screener 에러: {e}")

