"""
스캐너2-미: 미국장 진입 타이밍 필터 (스캐너1-미가 찾은 후보 대상)
조건: (1) 현재가 > 전일 일봉 고가
      (2) 전일 종가 > 200일 이동평균선
      (3) 현재가 > 오늘 프리마켓 고가
      (4) 현재가 > 오늘 오프닝레인지(ORB, 정규장 시작 후 첫 30분) 고가
          (9/1 수정: 원래 "현재가>오늘 일봉고가"였는데, 이 둘이 같은 API 스냅샷에서 동시에 계산되는 값이라
          신고가를 찍는 바로 그 순간엔 현재가==오늘고가가 되어 버려 논리적으로 ">"가 성립할 수 없는 구조적
          버그였음(실측 결과 84건 중 83건, 99% 실패). 대신 "장 시작~첫 30분 동안의 고가"를 오프닝레인지로
          한 번 확정(고정)시켜두고, 그 이후 현재가가 이 고정값을 실제로 돌파하는지 비교하도록 수정 —
          이렇게 하면 비교 기준과 현재가 사이에 실제 시간차가 생겨서 ">"가 의미를 가짐(ORB 브레이크아웃 기법).
          오프닝레인지가 아직 완성 안 됐으면(정규장 시작 후 30분 이내) 이 조건은 미확정으로 처리하고
          조건3/8과 동일하게 하드블록(그 시간대엔 진입신호 자체가 안 남 — 초반 변동성 구간이라 의도된 동작).
      (5) 거래량 확인: 조건3/4가 충족된 상태에서 최근 1분봉 거래량이 직전 20분 평균 거래량 대비 3배 이상
          (휩소/가짜돌파 필터링용. 1분봉 데이터를 못 받아오면 이 조건은 건너뛰고 가격조건만으로 판단하되
          결과에 "확인 못함"으로 표시한다)
      (6) 정배열: 20일선 > 50일선 > 200일선
      (7) 기울기: 오늘 50일선 > 5거래일 전 50일선
      (8) 현재가 > 오늘 VWAP(거래량가중평균가, 정규장 09:30 이후 1분봉 기준)
입력: scanner1_result.json의 matches (당일 스캐너1-미가 찾은 후보, 오래된 파일이면 무시)
결과: scanner2_result.json으로 저장 (깃허브 액션이 커밋 후 진입신호가 있을 때만 텔레그램으로 전송)

9/2 수정(API 요청 최적화): 후보 종목마다 개별적으로 yf.Ticker().history()/get_info()를 호출하던
방식(종목당 최대 3회 요청) 대신, 전체 후보 목록을 yf.download()로 한 번에 배치 조회하도록 변경.
깃허브 액션 러너는 공용 IP를 쓰기 때문에 종목 수가 많아지면(예전엔 8개로 캡이 걸려 있었지만
지금은 무제한) 순차적으로 개별 요청을 쏟아낼 경우 야후파이낸스 쪽 요청제한(429)에 걸릴 위험이
커진다는 지적을 받아, 배치 조회 + 짧은 재시도(백오프)로 방어함. 부수효과로 종목별 get_info()
호출 자체가 없어져서(현재가를 1분봉 마지막 종가로 대체) 필요없어진 "오늘 일봉고가" 필드 요구조건도
같이 제거함(ORB 도입 이후로 이 값 자체를 안 씀 — 예전엔 이 값이 없으면 후보가 그냥 에러 처리되던
잠재 버그였음).
"""
import json
import time
from datetime import datetime, timezone, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

VOLUME_MULTIPLIER = 3.0
VOLUME_LOOKBACK_MIN = 20
MA_SHORT = 20
MA_MID = 50
MA_LONG = 200
SLOPE_LOOKBACK_DAYS = 5
CANDIDATE_MAX_AGE_HOURS = 12
NY_TZ = ZoneInfo("America/New_York")
MARKET_OPEN = dtime(9, 30)
ORB_WINDOW_MIN = 30  # 오프닝레인지 = 정규장 시작 후 첫 30분

BATCH_RETRY_COUNT = 2
BATCH_RETRY_BACKOFF_SEC = 2.0
BATCH_GAP_SEC = 0.5  # 일봉 배치 조회와 분봉 배치 조회 사이 최소한의 텀


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


def _download_with_retry(symbols, **kwargs):
    """yf.download 배치 호출 + 실패시 짧은 재시도(429 등 일시적 요청제한 대비)."""
    last_exc = None
    for attempt in range(BATCH_RETRY_COUNT + 1):
        try:
            return yf.download(
                symbols, threads=True, progress=False, group_by="ticker", **kwargs
            )
        except Exception as e:
            last_exc = e
            if attempt < BATCH_RETRY_COUNT:
                time.sleep(BATCH_RETRY_BACKOFF_SEC * (attempt + 1))
    print(f"배치 다운로드 실패(재시도 소진): {last_exc}")
    return None


def _split_by_symbol(data, symbols):
    """yf.download 결과(멀티종목이면 컬럼이 종목별로 중첩됨)를 종목별 DataFrame 딕셔너리로 분리.
    종목이 1개뿐이어도 yfinance 버전에 따라 중첩될 수도/안 될 수도 있어서, symbols 개수가 아니라
    실제 반환된 컬럼이 MultiIndex인지(=종목별로 중첩됐는지)를 보고 판단한다."""
    out = {}
    if data is None or data.empty:
        return out
    is_multi = isinstance(data.columns, pd.MultiIndex)
    for sym in symbols:
        try:
            if is_multi:
                if sym not in data.columns.get_level_values(0):
                    continue
                df = data[sym]
            else:
                df = data
        except Exception:
            continue
        if df is None:
            continue
        df = df.dropna(how="all")
        if not df.empty:
            out[sym] = df
    return out


def fetch_daily_batch(symbols):
    if not symbols:
        return {}
    data = _download_with_retry(symbols, period="1y", interval="1d", auto_adjust=False)
    return _split_by_symbol(data, symbols)


def fetch_intraday_batch(symbols):
    if not symbols:
        return {}
    data = _download_with_retry(symbols, period="1d", interval="1m", prepost=True)
    return _split_by_symbol(data, symbols)


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


def compute_orb_high(intraday):
    """정규장 시작(09:30 NY) 후 첫 30분(오프닝레인지) 고가.
    오프닝레인지가 아직 안 끝났으면(지금이 09:30~10:00 사이) None(미확정)을 반환한다."""
    if intraday is None:
        return None
    try:
        idx_ny = intraday.index.tz_convert(NY_TZ)
    except Exception:
        return None

    now_ny = datetime.now(NY_TZ)
    orb_end_dt = datetime.combine(now_ny.date(), MARKET_OPEN, tzinfo=NY_TZ) + timedelta(minutes=ORB_WINDOW_MIN)
    if now_ny < orb_end_dt:
        return None  # 오프닝레인지 아직 진행중 — 미확정

    orb_end_time = orb_end_dt.timetz()
    opening_range = intraday[(idx_ny.time >= MARKET_OPEN) & (idx_ny.time < orb_end_time)]
    if opening_range.empty:
        return None
    return float(opening_range["High"].max())


def compute_vwap(intraday):
    """오늘 정규장(09:30 NY~) 1분봉 기준 VWAP. 정규장 데이터 없으면 None(확인불가)."""
    if intraday is None:
        return None
    try:
        idx_ny = intraday.index.tz_convert(NY_TZ)
    except Exception:
        return None
    regular = intraday[idx_ny.time >= dtime(9, 30)]
    if regular.empty:
        return None
    typical_price = (regular["High"] + regular["Low"] + regular["Close"]) / 3
    total_volume = float(regular["Volume"].sum())
    if not total_volume or total_volume <= 0:
        return None
    return float((typical_price * regular["Volume"]).sum() / total_volume)


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


def evaluate_symbol(symbol, daily_raw, intraday):
    if intraday is None or intraday.empty or "Close" not in intraday:
        return {"symbol": symbol, "error": "1분봉 데이터 없음"}

    close_series = intraday["Close"].dropna()
    if close_series.empty:
        return {"symbol": symbol, "error": "현재가 정보 없음"}
    price = float(close_series.iloc[-1])

    if daily_raw is None or daily_raw.empty:
        return {"symbol": symbol, "error": "일봉 히스토리 없음"}

    today_ny = datetime.now(NY_TZ).date()
    daily = daily_raw[daily_raw.index.date < today_ny]  # 오늘 진행중인 봉은 제외, 완성된 일봉만 사용
    if daily.empty:
        return {"symbol": symbol, "error": "일봉 히스토리 없음"}

    metrics = compute_daily_metrics(daily)
    if metrics is None:
        return {"symbol": symbol, "error": "일봉 히스토리 부족(200일 미만)"}

    premarket_high = compute_premarket_high(intraday)
    orb_high = compute_orb_high(intraday)
    volume_ok = check_volume_confirmation(intraday)  # True/False/None(확인불가)
    vwap = compute_vwap(intraday)

    cond1 = price > metrics["prev_day_high"]
    cond2 = metrics["prev_close"] > metrics["ma200"]
    cond3 = premarket_high is not None and price > premarket_high
    cond4 = orb_high is not None and price > orb_high
    cond6 = metrics["ma20"] > metrics["ma50"] > metrics["ma200"]
    cond7 = metrics["ma50_prev"] is not None and metrics["ma50"] > metrics["ma50_prev"]
    cond8 = vwap is not None and price > vwap

    price_conditions_met = cond1 and cond2 and cond3 and cond4 and cond6 and cond7 and cond8
    volume_checked = volume_ok is not None
    volume_confirmed = bool(volume_ok) if volume_checked else None

    entry_signal = price_conditions_met and (bool(volume_ok) or not volume_checked)

    return {
        "symbol": symbol,
        "price": price,
        "entry_signal": entry_signal,
        "price_conditions_met": price_conditions_met,
        "volume_confirmed": volume_confirmed,
        "vwap": vwap,
        "orb_high": orb_high,
        "conditions": {
            "1_prev_day_high_break": cond1,
            "2_prev_close_above_200ma": cond2,
            "3_premarket_high_break": cond3,
            "4_orb_high_break": cond4,
            "6_ma_alignment_20_50_200": cond6,
            "7_ma50_slope_up": cond7,
            "8_vwap_break": cond8,
        },
    }


def main():
    symbols = load_candidates()

    daily_batch = fetch_daily_batch(symbols)
    if symbols:
        time.sleep(BATCH_GAP_SEC)
    intraday_batch = fetch_intraday_batch(symbols)

    results = []
    errors = []
    for sym in symbols:
        try:
            r = evaluate_symbol(sym, daily_batch.get(sym), intraday_batch.get(sym))
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
