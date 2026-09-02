"""
스캐너1-미: 미국장 프리마켓 모멘텀 스크리너
조건: (1) 전일 종가 대비 +5% 이상 상승, (2) 주가 $3 이상,
      (3) 프리마켓 거래대금(현재가×거래량) $300,000 이상
          [9/2 수정: 원래 "프리마켓 거래량 50,000주 이상"(주식수 기준)이었는데, $3짜리가 5만주
          거래돼봤자 15만달러(약2억원)라 유동성 하한선으로는 너무 얇다는 지적(형배 피드백) →
          가격×거래량(=거래대금) 기준으로 바꿔서 저가주가 얇은 유동성으로 조건을 통과하는 걸
          막음. 이미 받아오는 가격/거래량 값만으로 계산하는 거라 API 추가 호출 없음]

추가로 매칭된 종목 각각에 "extreme_gap" 플래그를 붙인다(필터링은 안 함, 표시용):
프리마켓 갭이 +100% 이상이면 True — 이런 종목은 정규장 개장과 함께 재료 소멸로 급락(소위
"설거지")할 위험이 특히 크다는 지적(형배 피드백)에 따라, 텔레그램 메시지에 경고 태그를
붙여서 보는 사람이 더 보수적으로 판단하게 함(send_telegram.py에서 태그 렌더링).

yfinance 라이브러리로 야후 파이낸스의 스크리너(day_gainers, most_actives)를 후보군으로 모으고,
각 종목의 실시간 시세에서 프리마켓 필드를 읽어 조건을 검사한다.
결과는 scanner1_result.json 파일로 저장된다 (깃허브 액션이 이 파일을 커밋함).
"""
import json
import time
from datetime import datetime, timezone

import yfinance as yf

MIN_CHANGE_PCT = 5.0
MIN_PRICE = 3.0
MIN_PREMARKET_DOLLAR_VOLUME = 300000  # 프리마켓 거래대금(현재가×거래량) 하한선(달러)
EXTREME_GAP_PCT = 100.0  # 이 갭%을 넘으면 "설거지 리스크" 경고 태그(필터링은 안 함)

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
    if pre_volume is not None and (ref_price * pre_volume) < MIN_PREMARKET_DOLLAR_VOLUME:
        return None

    return {
        "symbol": symbol,
        "price": ref_price,
        "prev_close": prev_close,
        "change_pct": round(change_pct, 2),
        "premarket_volume": pre_volume,
        "is_premarket_data": pre_price is not None,
        "extreme_gap": change_pct >= EXTREME_GAP_PCT,
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
            "min_premarket_dollar_volume": MIN_PREMARKET_DOLLAR_VOLUME,
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
