"""
스캐너2-미: 미국장 진입 타이밍 필터 (스캐너1-미가 찾은 후보 대상)
조건: (1) 현재가 > 전일 일봉 고가
      (2) 전일 종가 > 200일 이동평균선
      (3) 현재가 > 오늘 프리마켓 고가
      (4) 현재가 > 오늘 일봉 고가
      (5) 거래량 확인: 조건3/4가 충족된 상태에서 최근 1분봉 거래량이 직전 20분 평균 거래량 대비 3배 이상
          (휩소/가짜돌파 필터링용. 1분봉 데이터를 못 받아오면 이 조건은 건너뛰고 가격조건만으로 판단하되
          결과에 "확인 못함"으로 표시한다)
      (6) 정배열: 20일선 > 50일선 > 200일선
      (7) 기울기: 오늘 50일선 > 5거래일 전 50일선
입력: scanner1_result.json의 matches (당일 스캐너1-미가 찾은 후보, 오래된 파일이면 무시)
결과: scanner2_result.json으로 저장 (깃허브 액션이 커밋 후 진입신호가 있을 때만 카카오로 전송)
"""
import json
from datetime import datetime, timezone, time as dtime
from zoneinfo import ZoneInfo

import yfinance as yf

VOLUME_MULTIPLIER = 3.0
VOLUME_LOOKBACK_MIN = 20
MA_SHORT = 20
MA_MID = 50
MA_LONG = 200
SLOPE_LOOKBACK_DAYS = 5
CANDIDATE_MAX_AGE_HOURS = 12
NY_TZ = ZoneInfo("America/New_York")


def load_candidates():
    try:
        with open("scanner1_result.json", encoding="utf-8") as f:
            result = json.load(f)
    except FileNotFoundError:
        return []

    updated_at = result.get("updated_at_utc", "")
    try:
        updated_dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return []

    age_hours = (datetime.now(timezone.utc) - updated_dt).total_seconds() / 3600
    if age_hours > CANDIDATE_MAX_AGE_HOURS:
        return []  # 전날 등 오래된 결과는 후보로 안 씀

    return [m["symbol"] for m in result.get("matches", [])]


def get_daily_frame(ticker):
    hist = ticker.history(period="1y", interval="1d", auto_adjust=False)
    if hist.empty:
        return None
    today_ny = datetime.now(NY_TZ).date()
    hist = hist[hist.index.date < today_ny]  # 오늘 진행중인 봉은 제외, 완성된 일봉만 사용
    return hist if not hist.empty else None


def compute_daily_metrics(daily):
    if len(daily) < MA_LONG:
        return None

    prev_high = float(daily["High"].iloc[-1])
    prev_close = float(daily["Close"].iloc[-1])

    ma20_series = daily["Close"].rolling(MA_SHORT).mean()
    ma50_series = daily["Close"].rolling(MA_MID).mean()
    ma200_series = daily["Close"].rolling(MA_LONG).mean()

    ma50_valid = ma50_series.dropna()
    if len(ma50_valid) <= SLOPE_LOOKBACK_DAYS:
        ma50_prev = None
    else:
        ma50_prev = float(ma50_series.iloc[-1 - SLOPE_LOOKBACK_DAYS])

    return {
        "prev_day_high": prev_high,
        "prev_close": prev_close,
        "ma20": float(ma20_series.iloc[-1]),
        "ma50": float(ma50_series.iloc[-1]),
        "ma200": float(ma200_series.iloc[-1]),
        "ma50_prev": ma50_prev,
    }


def get_intraday_frame(ticker):
    try:
        intraday = ticker.history(period="1d", interval="1m", prepost=True)
    except Exception:
        return None
    return intraday if not intraday.empty else None


def compute_premarket_high(intraday):
    if intraday is None:
        return None
    try:
        idx_ny = intraday.index.tz_convert(NY_TZ)
    except Exception:
        return None
    premarket = intraday[idx_ny.time < dtime(9, 30)]
    if premarket.empty:
        return None
    return float(premarket["High"].max())


def check_volume_confirmation(intraday):
    """최근 1분봉 거래량이 직전 20분 평균 대비 3배 이상인지. 데이터 부족/실패시 None(확인불가)."""
    if intraday is None or len(intraday) < VOLUME_LOOKBACK_MIN + 1:
        return None
    latest_vol = float(intraday["Volume"].iloc[-1])
    window = intraday["Volume"].iloc[-(VOLUME_LOOKBACK_MIN + 1):-1]
    avg_vol = float(window.mean())
    if not avg_vol or avg_vol <= 0:
        return None
    return latest_vol >= avg_vol * VOLUME_MULTIPLIER


def evaluate_symbol(symbol):
    ticker = yf.Ticker(symbol)
    info = ticker.get_info()

    price = info.get("regularMarketPrice") or info.get("currentPrice")
    day_high = info.get("regularMarketDayHigh") or info.get("dayHigh")
    if price is None or day_high is None:
        return {"symbol": symbol, "error": "현재가/오늘고가 정보 없음"}

    daily = get_daily_frame(ticker)
    if daily is None:
        return {"symbol": symbol, "error": "일봉 히스토리 없음"}

    metrics = compute_daily_metrics(daily)
    if metrics is None:
        return {"symbol": symbol, "error": "일봉 히스토리 부족(200일 미만)"}

    intraday = get_intraday_frame(ticker)
    premarket_high = compute_premarket_high(intraday)
    volume_ok = check_volume_confirmation(intraday)  # True/False/None(확인불가)

    cond1 = price > metrics["prev_day_high"]
    cond2 = metrics["prev_close"] > metrics["ma200"]
    cond3 = premarket_high is not None and price > premarket_high
    cond4 = price > day_high
    cond6 = metrics["ma20"] > metrics["ma50"] > metrics["ma200"]
    cond7 = metrics["ma50_prev"] is not None and metrics["ma50"] > metrics["ma50_prev"]

    price_conditions_met = cond1 and cond2 and cond3 and cond4 and cond6 and cond7
    volume_checked = volume_ok is not None
    volume_confirmed = bool(volume_ok) if volume_checked else None

    entry_signal = price_conditions_met and (bool(volume_ok) or not volume_checked)

    return {
        "symbol": symbol,
        "price": price,
        "entry_signal": entry_signal,
        "price_conditions_met": price_conditions_met,
        "volume_confirmed": volume_confirmed,
        "conditions": {
            "1_prev_day_high_break": cond1,
            "2_prev_close_above_200ma": cond2,
            "3_premarket_high_break": cond3,
            "4_today_high_break": cond4,
            "6_ma_alignment_20_50_200": cond6,
            "7_ma50_slope_up": cond7,
        },
    }


def main():
    symbols = load_candidates()

    results = []
    errors = []
    for sym in symbols:
        try:
            r = evaluate_symbol(sym)
            if "error" in r:
                errors.append(r)
            else:
                results.append(r)
        except Exception as e:
            errors.append({"symbol": sym, "error": str(e)})

    entries = [r for r in results if r["entry_signal"]]

    output = {
        "updated_at_utc": datetime.now(timezone.utc).isoformat(),
        "candidate_count": len(symbols),
        "checked_count": len(results),
        "error_count": len(errors),
        "entries": entries,
        "all_results": results,
        "errors": errors[:10],
    }

    with open("scanner2_result.json", "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(f"후보 종목 수: {len(symbols)} / 진입신호: {len(entries)}건 / 에러: {len(errors)}건")
    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()

