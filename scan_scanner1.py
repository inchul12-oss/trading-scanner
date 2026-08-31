"""
스캐너1-미: 미국장 프리마켓 모멘텀 스크리너
조건: (1) 전일 종가 대비 +5% 이상 상승, (2) 주가 $3 이상, (3) 프리마켓 거래량 50,000주 이상

yfinance 라이브러리로 야후 파이낸스의 스크리너(day_gainers, most_actives)를 후보군으로 모으고,
각 종목의 실시간 시세에서 프리마켓 필드를 읽어 3개 조건을 검사한다.
결과는 scanner1_result.json 파일로 저장된다 (깃허브 액션이 이 파일을 커밋함).
"""
import json
import time
from datetime import datetime, timezone

import yfinance as yf

MIN_CHANGE_PCT = 5.0
MIN_PRICE = 3.0
MIN_PREMARKET_VOLUME = 50000

SCREENER_QUERIES = ["day_gainers", "most_actives"]

def get_candidate_symbols():
    symbols = set()
    for query in SCREENER_QUERIES:
        try:
            res = yf.screen(query, count=100)
            quotes = res.get("quotes", [])
            for q in quotes:
                sym = q.get("symbol")
                if sym:
                    symbols.add(sym)
            print(f"[ok] screener '{query}': {len(quotes)}건")
        except Exception as e:
            print(f"[warn] screener '{query}' 실패: {e}")
    return list(symbols)

def passes_filters(symbol, info):
    price = info.get("regularMarketPrice")
    prev_close = info.get("previousClose") or info.get("regularMarketPreviousClose")
    pre_price = info.get("preMarketPrice")
    pre_volume = info.get("preMarketVolume")

    ref_price = pre_price if pre_price is not None else price
    if ref_price is None or not prev_close:
        return None

    change_pct = (ref_price - prev_close) / prev_close * 100
    if change_pct < MIN_CHANGE_PCT:
        return None
    if ref_price < MIN_PRICE:
        return None
    if pre_volume is not None and pre_volume < MIN_PREMARKET_VOLUME:
        return None

    return {
        "symbol": symbol,
        "price": ref_price,
        "prev_close": prev_close,
        "change_pct": round(change_pct, 2),
        "premarket_volume": pre_volume,
        "is_premarket_data": pre_price is not None,
    }

def main():
    symbols = get_candidate_symbols()
    print(f"후보 종목 수: {len(symbols)}")

    matches = []
    errors = []
    for sym in symbols:
        try:
            info = yf.Ticker(sym).get_info()
            m = passes_filters(sym, info)
            if m:
                matches.append(m)
        except Exception as e:
            errors.append(f"{sym}: {e}")
        time.sleep(0.3)  # 과도한 연속 호출 방지

    matches.sort(key=lambda x: x["change_pct"], reverse=True)

    result = {
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "conditions": {
            "min_change_pct": MIN_CHANGE_PCT,
            "min_price": MIN_PRICE,
            "min_premarket_volume": MIN_PREMARKET_VOLUME,
        },
        "candidate_count": len(symbols),
        "error_count": len(errors),
        "matches": matches,
    }

    with open("scanner1_result.json", "w") as f:
        json.dump(result, f, indent=2, ensure_ascii=False)

    print(f"매칭된 종목 수: {len(matches)}")
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if errors:
        print(f"[info] 에러난 종목 {len(errors)}개 (일부만 표시):", errors[:5])

if __name__ == "__main__":
    main()
