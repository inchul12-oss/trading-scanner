"""
스캐너2-미: 미국장 진입 타이밍 필터 (스캐너1-미가 찾은 후보 대상)

9/2 조건 재설계(하드게이트+액션트리거 구조로 전면 개편):
예전엔 조건 8개를 전부 AND로 걸었는데(200일선 포함), 조건 하나하나가 개별적으로는
합리적이어도 8개를 다 곱하면 통과율이 지나치게 낮아지고(예: 조건당 65% 통과율이면
0.65^7 ≈ 5%), 특히 200일선 조건은 신규상장주(200거래일 미만)를 원천적으로 평가
대상에서 제외시켜버리는 구조적 문제가 있었음(실측: MMED 등). 조건들을 역할별로
분리해서 "추세/유동성 필수조건(하드게이트)"과 "돌파 시그널(액션트리거, 3개중 2개)"로
재구성함. 하락 방어는 이미 포지션 진입 후 -5% 하드스탑/-3% 서킷브레이커(scan_scanner3.py)가
따로 담당하고 있어서, 200일선을 진입 전 필터로 또 거는 건 중복이라고 판단해 제거함.

■ 하드게이트 (4개, 전부 필수 — 하나라도 스킵되면 진입신호 안 냄)
  (1) 20일 이동평균 > 50일 이동평균 (단기 추세가 살아있는가)
  (2) 오늘 50일선 > 5거래일 전 50일선 (추세가 유지/상승 중인가, 기울기)
  (3) 현재가 > 오늘 VWAP(거래량가중평균가, 정규장 09:30 이후 1분봉 기준)
  (4) 거래량 확인: 최근 1분봉 거래량이 "지금까지 쌓인 1분봉(최대 20개) 평균" 대비
      3배 이상. 예전엔 1분봉 데이터가 20개 미만이면 그냥 스킵(가격조건만으로 판단)
      했는데, 이제는 필수 조건으로 바꾸되 장 시작 직후처럼 쌓인 1분봉이 적을 땐
      "동적 윈도우"(있는 만큼, 최대 20개)로 평균을 내서 항상 확인 가능하게 함
      (휩소/가짜돌파 필터링 역할은 그대로 유지하면서 스킵을 없앰).

■ 액션 트리거 (3개 중 2개 이상 충족하면 통과)
  (A) 현재가 > 전일 일봉 고가
  (B) 현재가 > 오늘 프리마켓 고가
  (C) 현재가 > 오늘 오프닝레인지(ORB, 정규장 시작 후 첫 30분) 고가
      (9/1 수정: 원래 "현재가>오늘 일봉고가"였는데, 이 둘이 같은 API 스냅샷에서 동시에 계산되는 값이라
      신고가를 찍는 바로 그 순간엔 현재가==오늘고가가 되어 버려 논리적으로 ">"가 성립할 수 없는 구조적
      버그였음(실측 결과 84건 중 83건, 99% 실패). 대신 "장 시작~첫 30분 동안의 고가"를 오프닝레인지로
      한 번 확정(고정)시켜두고, 그 이후 현재가가 이 고정값을 실제로 돌파하는지 비교하도록 수정 —
      이렇게 하면 비교 기준과 현재가 사이에 실제 시간차가 생겨서 ">"가 의미를 가짐(ORB 브레이크아웃 기법).
      오프닝레인지가 아직 완성 안 됐으면(정규장 시작 후 30분 이내) 이 조건은 미확정으로 처리함.)

진입신호 = 하드게이트 전부 통과 AND 액션트리거 2개 이상 충족

9/2 조건 재설계 이전에는 200일선 조건(전일 종가>200일선) + 정배열(20>50>200일선)의
200일선 다리가 있었지만 전부 삭제. 이에 따라 필요한 최소 일봉 데이터 요구량도
200일치→약 55일치(50일선+5일 기울기 계산분)로 줄어들어, 신규상장주도 정상적으로
평가 테이블에 오르게 됨.

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

9/3 추가(재진입 쿨다운, 휩소 방지): scan_scanner3.py의 positions.json은 청산된 포지션도
status="closed"로 남겨두고 지우지 않으므로, 오늘(NY 날짜) 이미 청산된 이력이 있는 종목은
청산 사유를 불문하고 오늘 안에는 다시 진입신호를 내지 않도록 후보에서 제외한다. 손절 직후
같은 종목이 바로 다시 걸려서 반복 진입/손절되는(계좌가 갈리는) 걸 막기 위함. positions.json이
이미 매매이력 그 자체이므로 별도 history 파일은 만들지 않음(국장은 청산 시 포지션 줄을
지우는 구조라 국장에만 별도 history-kr.md를 둠).

9/3 저녁 수정(진입신호 중복 버그 픽스): 위 재진입 쿨다운이 "오늘 청산된(status=closed) 종목"만
제외하고 "지금 보유중인(status=open) 종목"은 빼먹고 있었음. scan_scanner3.py의
open_new_positions()는 이미 열린 종목이면 실제 포지션은 중복으로 안 만들게 막아주지만, 이 파일의
entries(=텔레그램 진입신호)는 그 체크가 없어서 포지션을 계속 들고 있는 중에도 조건을 통과할 때마다
매번 "새 진입신호"로 잡혀 텔레그램이 중복 발송되고 있었음(9/3 실측: COMP 3회, XXI 2회, 전부
status=open 상태로 청산 없이 중복 발생 확인). load_cooldown_symbols()의 제외 조건에
status=="open"(오늘 날짜 상관없이 무조건)을 추가해서 수정함.

9/3 밤 추가(힘겨루기 종목 배제): WPP가 하루 안에 진입→"돌파캔들저점 이탈"청산→재진입→같은
사유로 재청산을 반복하는 사례 발견. 조사해보니 진짜 원인은 scan_scanner3.py가 "오늘 이미
청산된" 종목을 재오픈 방지 체크에서 빼먹은 별도 버그였음(scan_scanner3.py 쪽에서 같이 수정,
아래 참고) — 다만 그 버그와 별개로, 저점 부근에서 진짜로 힘겨루기(반복 이탈)하는 종목을
걸러내는 안전장치도 함께 두기로 함(인철님 확정): 손절 조건(돌파캔들저점이탈) 자체를 완화하면
가짜돌파 전체에 대한 방어가 무뎌지므로, 대신 "돌파캔들저점 이탈" 사유로 최근 5거래일 안에
2회 이상 청산된 종목만 당분간 후보에서 제외하는 방식(load_cooldown_symbols()에 통합).
"""
import json
import time
from datetime import datetime, timezone, time as dtime, timedelta
from zoneinfo import ZoneInfo

import pandas as pd
import yfinance as yf

VOLUME_MULTIPLIER = 3.0
VOLUME_LOOKBACK_MIN = 20  # 거래량 확인용 기준(평균) 윈도우의 최대 크기(그 이하로 쌓였으면 쌓인 만큼만 사용)
VOLUME_RECENT_WINDOW_MIN = 15  # 9/3 수정: 스캔 주기(5~15분)를 커버하기 위한 "스파이크 후보 구간" 크기(분)
MA_SHORT = 20
MA_MID = 50
SLOPE_LOOKBACK_DAYS = 5
MIN_DAILY_BARS = MA_MID + SLOPE_LOOKBACK_DAYS  # 50일선 + 5일 기울기 계산에 필요한 최소 일봉 수(55일)
CANDIDATE_MAX_AGE_HOURS = 12
NY_TZ = ZoneInfo("America/New_York")
POSITIONS_FILE = "positions.json"  # 9/3 추가: 재진입 쿨다운 판단용(스캐너3-미가 커밋하는 파일 그대로 읽기만 함)
MARKET_OPEN = dtime(9, 30)
ORB_WINDOW_MIN = 30  # 오프닝레인지 = 정규장 시작 후 첫 30분
ACTION_TRIGGER_MIN_COUNT = 2  # 액션 트리거 3개 중 최소 몇 개 이상 충족해야 하는지

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


CHOP_LOOKBACK_DAYS = 5  # 9/3 밤 추가: 힘겨루기(반복 돌파캔들저점 이탈) 판정용 최근 일수
CHOP_EXIT_REASON = "돌파캔들저점 이탈"
CHOP_MIN_COUNT = 2  # 이 사유로 최근 CHOP_LOOKBACK_DAYS일 안에 이 횟수 이상 청산되면 당분간 배제


def load_cooldown_symbols():
    """9/3 추가, 9/3 저녁 수정(진입신호 중복 버그 픽스), 9/3 밤 수정(힘겨루기 종목 배제 추가):
    재진입 쿨다운 + 포지션 보유중 종목 + 반복 저점이탈(힘겨루기) 종목 제외용.
    positions.json을 읽기만 하고 쓰지는 않음(쓰기는 scan_scanner3.py 담당).
    - status=="open": 오늘 날짜 상관없이 무조건 제외한다. 이미 포지션을 들고 있는 종목인데 조건을
      다시 통과할 때마다 "새 진입신호"로 잡혀서 텔레그램이 중복 발송되는 버그를 막기 위함
      (9/3 실측: COMP 3회, XXI 2회, 전부 청산 없이 open 상태에서 중복 발생 확인).
    - status=="closed" and 오늘(NY) 청산: 재진입 쿨다운(휩소 방지, 기존 로직 유지).
    - 최근 CHOP_LOOKBACK_DAYS일 안에 "돌파캔들저점 이탈" 사유로 CHOP_MIN_COUNT회 이상 청산된 종목:
      저점 부근에서 진입-이탈이 반복되는 힘겨루기 종목으로 판단해 당분간 배제한다(9/3 밤, WPP가
      하루 안에 진입→저점이탈청산→재진입→저점이탈청산을 반복한 사례가 계기 — 이건 사실 scan_
      scanner3.py의 재오픈 버그(아래 별도 수정)가 주원인이었지만, 손절 조건 자체를 물러지게 하는
      대신 "반복적으로 흔들리는 종목만 걸러낸다"는 방향은 그대로 유지하기로 함)."""
    try:
        with open(POSITIONS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return set()
    today_ny_date = datetime.now(NY_TZ).date()
    today_ny = str(today_ny_date)
    cutoff_date = today_ny_date - timedelta(days=CHOP_LOOKBACK_DAYS)

    cooldown = set()
    chop_counts = {}
    for p in data.get("positions", []):
        symbol = p.get("symbol")
        status = p.get("status")
        entry_date_ny = p.get("entry_date_ny")

        if status == "open":
            cooldown.add(symbol)
        elif status == "closed" and entry_date_ny == today_ny:
            cooldown.add(symbol)

        if status == "closed" and CHOP_EXIT_REASON in (p.get("exit_reasons") or []):
            try:
                entry_date = datetime.strptime(entry_date_ny, "%Y-%m-%d").date()
            except (ValueError, TypeError):
                entry_date = None
            if entry_date is not None and entry_date >= cutoff_date:
                chop_counts[symbol] = chop_counts.get(symbol, 0) + 1

    for symbol, count in chop_counts.items():
        if count >= CHOP_MIN_COUNT:
            cooldown.add(symbol)

    return cooldown


def _download_with_retry(symbols, **kwargs):
    """yf.download 배치 호출 + 실패시 짧은 재시도(429 등 일시적 요청제한 대비).
    threads=False: 병렬(threads=True)로 돌리면 yfinance 내부 SQLite 캐시에 동시접근하면서
    가끔 "database is locked"로 개별 종목이 실패하는 걸 실측으로 확인함(9/2) — 배치 자체가
    이미 충분히 빠르고(수 초), 어차피 목표가 "동시 요청 줄이기"라 순차(threads=False)로 바꿔서
    이 충돌 자체를 없앰."""
    last_exc = None
    for attempt in range(BATCH_RETRY_COUNT + 1):
        try:
            return yf.download(
                symbols, threads=False, progress=False, group_by="ticker", **kwargs
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
    if len(daily) < MIN_DAILY_BARS:
        return None

    prev_high = float(daily["High"].iloc[-1])
    prev_close = float(daily["Close"].iloc[-1])

    ma20_series = daily["Close"].rolling(MA_SHORT).mean()
    ma50_series = daily["Close"].rolling(MA_MID).mean()

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
    """최근 VOLUME_RECENT_WINDOW_MIN분(기본 15분) 안의 1분봉 중 어느 하나라도, 그보다 이전 구간
    (최대 20개 1분봉) 평균 대비 VOLUME_MULTIPLIER배(기본 3배) 이상 거래량이 있었는지 확인한다.
    9/3 수정: 예전엔 "가장 최근 1분봉 딱 하나"만 봤는데, 실제 거래량 스파이크는 보통 1분 정도만
    반짝하고 지나가는 반면 scanner2는 5~15분 간격으로 도는 구조라, 딱 그 1분을 스캔 타이밍이
    맞춰서 잡을 확률이 낮아 사실상 진입신호가 거의 안 뜨는 문제가 있었음(9/3 실측: 58회 스캔 중
    거래량 조건 통과 2회뿐). 그래서 "방금 그 순간"이 아니라 "최근 15분 사이에 스파이크가 있었나"로
    넓힘. 비교 기준(평균)은 스파이크 후보 구간보다 더 이전 데이터로만 계산해서, 스파이크 자체가
    평균에 섞여 기준선을 흐리는 걸 방지(그러면 계속 통과가 안 될 수 있음)."""
    if intraday is None or len(intraday) < 2:
        return None
    n = len(intraday)
    recent_size = min(n - 1, VOLUME_RECENT_WINDOW_MIN)  # 스파이크 후보 구간(최근 N분)
    baseline_available = n - recent_size
    baseline_size = min(baseline_available, VOLUME_LOOKBACK_MIN)
    if baseline_size <= 0:
        return None
    baseline = intraday["Volume"].iloc[-(recent_size + baseline_size):-recent_size]
    avg_vol = float(baseline.mean())
    if not avg_vol or avg_vol <= 0:
        return None
    recent = intraday["Volume"].iloc[-recent_size:]
    max_recent_vol = float(recent.max())
    return max_recent_vol >= avg_vol * VOLUME_MULTIPLIER


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
        return {"symbol": symbol, "error": f"일봉 히스토리 부족({MIN_DAILY_BARS}일 미만)"}

    premarket_high = compute_premarket_high(intraday)
    orb_high = compute_orb_high(intraday)
    volume_ok = check_volume_confirmation(intraday)  # True/False/None(확인불가)
    vwap = compute_vwap(intraday)

    # 하드게이트 (4개, 전부 필수 — 미확정(None)이면 통과 실패로 처리)
    gate_ma_alignment = metrics["ma20"] > metrics["ma50"]
    gate_ma_slope = metrics["ma50_prev"] is not None and metrics["ma50"] > metrics["ma50_prev"]
    gate_vwap = vwap is not None and price > vwap
    gate_volume = volume_ok is True
    hard_gate_passed = gate_ma_alignment and gate_ma_slope and gate_vwap and gate_volume

    # 액션 트리거 (3개 중 2개 이상)
    trigger_prev_day_high = price > metrics["prev_day_high"]
    trigger_premarket_high = premarket_high is not None and price > premarket_high
    trigger_orb_high = orb_high is not None and price > orb_high
    trigger_count = sum([trigger_prev_day_high, trigger_premarket_high, trigger_orb_high])
    action_trigger_passed = trigger_count >= ACTION_TRIGGER_MIN_COUNT

    entry_signal = hard_gate_passed and action_trigger_passed
    volume_confirmed = volume_ok  # True/False/None, 텔레그램/카카오 메시지에 그대로 사용됨

    return {
        "symbol": symbol,
        "price": price,
        "entry_signal": entry_signal,
        "hard_gate_passed": hard_gate_passed,
        "action_trigger_count": trigger_count,
        "action_trigger_passed": action_trigger_passed,
        "volume_confirmed": volume_confirmed,
        "vwap": vwap,
        "orb_high": orb_high,
        "hard_gate": {
            "1_ma20_above_ma50": gate_ma_alignment,
            "2_ma50_slope_up": gate_ma_slope,
            "3_vwap_break": gate_vwap,
            "4_volume_confirmed": gate_volume,
        },
        "action_trigger": {
            "A_prev_day_high_break": trigger_prev_day_high,
            "B_premarket_high_break": trigger_premarket_high,
            "C_orb_high_break": trigger_orb_high,
        },
    }


def main():
    symbols = load_candidates()

    # 9/3 추가, 9/3 저녁 수정, 9/3 밤 수정: 재진입 쿨다운 — 오늘 이미 청산됐거나 지금 보유중이거나
    # 최근 반복적으로 저점이탈(힘겨루기)한 종목은 후보에서 제외
    cooldown = load_cooldown_symbols()
    cooldown_excluded = [s for s in symbols if s in cooldown]
    if cooldown_excluded:
        symbols = [s for s in symbols if s not in cooldown]
        print(f"재진입 쿨다운으로 제외된 종목({len(cooldown_excluded)}건): {cooldown_excluded}")

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
        "cooldown_excluded": cooldown_excluded,
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
