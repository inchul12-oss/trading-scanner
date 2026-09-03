"""
스캐너3-미: 매도/손절 신호 + 1차 익절 신호 체크 (스캐너2-미가 낸 진입신호를 가상 포지션으로 추적)

청산 조건 (OR, 하나라도 걸리면 매도신호 - 포지션 종료):
  (1) 돌파 캔들 저점 이탈: 진입신호가 발생한 그 1분봉의 저가 아래로 현재가 하락
  (2) VWAP/20일선 이탈: 진입 당일이면 오늘 VWAP, 다음날부터는 20일 이동평균선
  (3) 스탑: +10%(2R) 도달 전까지는 진입가 대비 -5% 고정 하드스탑. +10%를 한 번이라도
      찍으면 그 순간부터 "최고가 대비 -7%" 트레일링스탑으로 전환되고(9/2 추가),
      본절(진입가) 밑으로는 절대 안 내려감 — 급등 후 되돌림으로 이익을 다 반납하는 걸
      막기 위함. 최고가는 매 체크마다 갱신되는 값이라 신고가를 계속 찍는 한 스탑도
      같이 따라 올라감(체이스는 안 하고 딱 한 번 -7%p만 유지). 20일선 조건은 며칠에
      걸쳐 이어지는 종목엔 유효하지만, 당일 급등주(예: 하루 만에 +70%대까지 간 사례도
      실제로 있었음)엔 20일선이 너무 느려서 못 따라오므로 별도 방어선으로 둠.
  (4) 직선급락 서킷브레이커: 최근 5분(1분봉 5개) 사이 -3% 이상 급락 시 "긴급" 태그로 즉시 청산
      (참고: 5분 폴링 주기 안에서의 최선의 감지이며, 호가창이 통째로 증발하는 진짜 유동성
       붕괴 상황에서의 체결까지 보장하지는 못한다 - 그런 종목은 실제 계좌에 시장가 스탑주문을
       병행할 것)
  (5) 타임스탑(데드머니컷, 9/3 밤 추가): 진입 후 TIME_STOP_TRADING_DAYS(3)거래일이 지났는데도
      손익률이 TIME_STOP_MIN_RETURN_PCT(+3%) 미만이면 강제청산. 손실도 익절도 아닌 채로 시간만
      끄는 포지션을 잘라내 계좌 자리를 비워주는 스윙매매 기회비용 관리용 조건(인철님 요청).
      거래일수는 월~금만 카운트하는 근사치(미국 공휴일 캘린더 없음, 다른 조건들과 동일한 수준의
      근사). "+3% 미만"은 완전 횡보(0~3%)뿐 아니라 아직 하드스탑엔 안 걸린 소폭 마이너스도
      포함 — 이 프로젝트에선 둘 다 "죽은 돈"으로 취급하기로 함(인철님 확정).

1차 익절 신호 (포지션은 종료하지 않음, 알림만 - 하이브리드 익절 설계 8/31 추가, 9/2 트레일링 연동):
  진입가 대비 +10%(하드스탑 -5%의 2배 = 2R) 도달 시 1회만 "절반 익절 고려" 알림.
  동시에 이 시점부터 위 (3)의 하드스탑이 트레일링스탑으로 전환됨. 포지션 자체는 계속
  살아있고 나머지 절반을 트레일링으로 태우는 하이브리드 방식(Qullamaggie 등 유명
  브레이크아웃 트레이더들의 공통 패턴을 참고함 - 고정목표 전량매도도, 트레일링만으로
  전량관리도 아닌, 일부는 미리 챙기고 나머지는 추세이탈까지 태우는 방식).

입력: scanner2_result.json의 entries (당일 스캐너2-미가 낸 진입신호, 오래된 파일이면 무시)
상태: positions.json (오픈/청산 포지션 영속 기록, 깃허브 액션이 커밋해서 유지)
결과: scanner3_result.json (이번 실행에서 새로 청산된 포지션 + 새로 뜬 1차익절신호, 있을 때만 텔레그램으로 전송)

9/3 밤 수정(재오픈 버그 픽스): open_new_positions()가 "지금 open인 종목"만 재오픈 방지 대상으로
삼아서, scanner2_result.json이 아직 갱신되기 전(이 스크립트는 5분 고정주기, scan_scanner2.py는
5~30분 가변주기라 서로 어긋나는 구간이 있음) 같은 entries로 이 스크립트가 재실행되면 "방금
청산한 종목"을 같은 entry_price/entry_time_utc로 즉시 재오픈해버리는 버그가 있었음(실측: WPP가
같은 entry_time_utc로 하루에 3번 열림/닫힘 반복 — 인철님이 "진입신호 자꾸 왔다갔다한다"고
지적해서 발견). "오늘 이미 청산된(status=closed, entry_date_ny=오늘)" 종목도 재오픈 방지
대상에 포함시켜 수정함.
"""
import json
from datetime import datetime, timezone, time as dtime, timedelta
from zoneinfo import ZoneInfo

import yfinance as yf

HARD_STOP_PCT = -0.05
PARTIAL_PROFIT_PCT = 0.10  # 1차 익절 알림 임계값(진입가 대비 +10%, 하드스탑의 2R) - 트레일링스탑 발동 기준도 겸함
TRAIL_STOP_PCT = 0.07  # PARTIAL_PROFIT_PCT 도달 이후: 최고가 대비 -7% 트레일링(본절 밑으로는 안 내려감)
CIRCUIT_BREAKER_PCT = -0.03
CIRCUIT_BREAKER_LOOKBACK_MIN = 5
MA_SHORT = 20
CANDIDATE_MAX_AGE_HOURS = 12
TIME_STOP_TRADING_DAYS = 3  # 9/3 밤 추가: 타임스탑(데드머니컷) 기준 거래일수
TIME_STOP_MIN_RETURN_PCT = 0.03  # 이 거래일수가 지났는데 손익률이 이 값 미만이면 강제청산
NY_TZ = ZoneInfo("America/New_York")

POSITIONS_FILE = "positions.json"
SCANNER2_RESULT_FILE = "scanner2_result.json"
RESULT_FILE = "scanner3_result.json"


def load_positions():
    try:
        with open(POSITIONS_FILE, encoding="utf-8") as f:
            data = json.load(f)
    except FileNotFoundError:
        return []
    return data.get("positions", [])


def load_scanner2_entries():
    try:
        with open(SCANNER2_RESULT_FILE, encoding="utf-8") as f:
            result = json.load(f)
    except FileNotFoundError:
        return [], None

    updated_at = result.get("updated_at_utc", "")
    try:
        updated_dt = datetime.fromisoformat(updated_at.replace("Z", "+00:00"))
    except ValueError:
        return [], None

    age_hours = (datetime.now(timezone.utc) - updated_dt).total_seconds() / 3600
    if age_hours > CANDIDATE_MAX_AGE_HOURS:
        return [], None  # 오래된 결과는 새 진입으로 안 씀

    return result.get("entries", []), updated_at


def get_intraday_frame(ticker):
    try:
        intraday = ticker.history(period="1d", interval="1m", prepost=True)
    except Exception:
        return None
    return intraday if not intraday.empty else None


def get_daily_frame(ticker):
    hist = ticker.history(period="6mo", interval="1d", auto_adjust=False)
    if hist.empty:
        return None
    today_ny = datetime.now(NY_TZ).date()
    hist = hist[hist.index.date < today_ny]
    return hist if not hist.empty else None


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


def compute_recent_drop_pct(intraday):
    """최근 CIRCUIT_BREAKER_LOOKBACK_MIN분 사이 종가 기준 등락률. 데이터 부족시 None."""
    if intraday is None or len(intraday) < CIRCUIT_BREAKER_LOOKBACK_MIN + 1:
        return None
    recent = intraday["Close"].iloc[-(CIRCUIT_BREAKER_LOOKBACK_MIN + 1):]
    ref_price = float(recent.iloc[0])
    cur_price = float(recent.iloc[-1])
    if ref_price <= 0:
        return None
    return (cur_price - ref_price) / ref_price


def find_breakout_candle_low(intraday, entry_time_utc):
    """진입신호 시점과 가장 가까운 1분봉의 저가. 못 찾으면 None(하드스탑/VWAP만으로 판단)."""
    if intraday is None or not entry_time_utc:
        return None
    try:
        entry_dt = datetime.fromisoformat(entry_time_utc.replace("Z", "+00:00"))
    except ValueError:
        return None
    try:
        idx_utc = intraday.index.tz_convert("UTC")
    except Exception:
        return None
    on_or_after = intraday[idx_utc >= entry_dt]
    bar = on_or_after.iloc[0] if not on_or_after.empty else intraday.iloc[-1]
    return float(bar["Low"])


def trading_days_elapsed(entry_date_ny_str, today_ny_date):
    """entry_date_ny부터 오늘까지 지난 거래일수(월~금만 카운트, 미국 공휴일 캘린더는 없음 —
    근사치, 다른 곳들도 같은 수준으로 근사함). 진입 당일=0, 다음 거래일=1 ... 식으로 카운트.
    파싱 실패시 None(타임스탑 판정 스킵)."""
    try:
        entry_date = datetime.strptime(entry_date_ny_str, "%Y-%m-%d").date()
    except (ValueError, TypeError):
        return None
    if today_ny_date < entry_date:
        return None
    count = 0
    d = entry_date
    while d < today_ny_date:
        d += timedelta(days=1)
        if d.weekday() < 5:  # 0=월 ... 4=금
            count += 1
    return count


def open_new_positions(positions, entries, entry_time_utc):
    today_ny = str(datetime.now(NY_TZ).date())
    open_symbols = {p["symbol"] for p in positions if p["status"] == "open"}
    # 9/3 밤 추가: 오늘 이미 청산된 종목도 재오픈 방지 대상에 포함.
    # 예전엔 "지금 open인 종목"만 걸렀는데, scan_scanner2.py(5~30분 주기)보다 이 스크립트(5분
    # 고정주기)가 더 자주 도는 구간에서는 scanner2_result.json이 아직 갱신 전이라 "방금 청산한
    # 종목"이 여전히 entries에 남아있는 채로 이 함수가 다시 호출될 수 있음 — 그 경우 open_symbols
    # 에는 이미 없으니(청산돼서 open이 아니게 됨) 같은 종목을 같은 entry_price/entry_time으로
    # 즉시 재오픈해버리는 버그가 있었음(실측: WPP가 같은 entry_time_utc로 하루에 3번 열림/닫힘
    # 반복 — scan_scanner2.py의 재진입 쿨다운은 다음 scanner2 실행 전까지는 이 재오픈을 못 막음).
    closed_today_symbols = {
        p["symbol"] for p in positions
        if p["status"] == "closed" and p.get("entry_date_ny") == today_ny
    }
    blocked_symbols = open_symbols | closed_today_symbols

    for e in entries:
        symbol = e.get("symbol")
        price = e.get("price")
        if not symbol or price is None or symbol in blocked_symbols:
            continue
        try:
            ticker = yf.Ticker(symbol)
            intraday = get_intraday_frame(ticker)
            candle_low = find_breakout_candle_low(intraday, entry_time_utc)
        except Exception:
            candle_low = None

        positions.append({
            "symbol": symbol,
            "entry_price": price,
            "entry_time_utc": entry_time_utc,
            "entry_date_ny": today_ny,
            "breakout_candle_low": candle_low,
            "peak_price": price,  # 9/2 추가: 트레일링스탑 계산용 최고가 추적(시작값=진입가)
            "status": "open",
            "partial_profit_alerted": False,
        })
        blocked_symbols.add(symbol)

    return positions


def evaluate_position(pos):
    symbol = pos["symbol"]
    ticker = yf.Ticker(symbol)
    info = ticker.get_info()
    price = info.get("regularMarketPrice") or info.get("currentPrice")
    if price is None:
        return {"error": "현재가 정보 없음"}

    intraday = get_intraday_frame(ticker)
    today_ny_date = datetime.now(NY_TZ).date()
    today_ny = str(today_ny_date)
    same_day = (today_ny == pos.get("entry_date_ny"))

    reasons = []

    candle_low = pos.get("breakout_candle_low")
    if candle_low is not None and price < candle_low:
        reasons.append("돌파캔들저점 이탈")

    if same_day:
        vwap = compute_vwap(intraday)
        if vwap is not None and price < vwap:
            reasons.append("VWAP 이탈")
    else:
        daily = get_daily_frame(ticker)
        if daily is not None and len(daily) >= MA_SHORT:
            ma20 = float(daily["Close"].rolling(MA_SHORT).mean().iloc[-1])
            if price < ma20:
                reasons.append("20일선 이탈")

    entry_price = pos["entry_price"]
    pnl_pct = (price - entry_price) / entry_price if entry_price else 0.0

    # 9/2 추가: 최고가 갱신 + 트레일링스탑. +10%(PARTIAL_PROFIT_PCT)를 한 번이라도
    # 찍으면(이번 체크 포함) 그 순간부터 고정 하드스탑 대신 "최고가 대비 -TRAIL_STOP_PCT%"
    # 트레일링스탑으로 전환하고, 스탑이 본절(진입가) 밑으로는 절대 안 내려가게 max()로 고정.
    peak_price = max(pos.get("peak_price", entry_price), price)
    trail_active = bool(pos.get("partial_profit_alerted")) or pnl_pct >= PARTIAL_PROFIT_PCT

    if trail_active:
        trail_stop_price = max(entry_price, peak_price * (1 - TRAIL_STOP_PCT))
        if price <= trail_stop_price:
            reasons.append(
                f"트레일링스탑(최고가 {peak_price:.2f} 대비 -{TRAIL_STOP_PCT * 100:.0f}%, "
                f"손익 {pnl_pct * 100:.1f}%)"
            )
    else:
        if pnl_pct <= HARD_STOP_PCT:
            reasons.append(f"하드스탑({pnl_pct * 100:.1f}%)")

    # 9/3 밤 추가: 타임스탑(데드머니컷) — 스윙매매 기회비용 관리용. 손실도 익절도 아닌 채로
    # 시간만 끄는 포지션(진입 후 TIME_STOP_TRADING_DAYS거래일이 지났는데 손익률이 아직
    # TIME_STOP_MIN_RETURN_PCT 미만)을 강제청산해서 계좌 자리를 비워줌. "완전 횡보(0~3%)"뿐
    # 아니라 소폭 마이너스인데 아직 하드스탑엔 안 걸린 경우도 포함(둘 다 "죽은 돈"으로 봄,
    # 인철님 확정). 이미 익절굌도(+10%↑, 트레일링 전환)에 들어간 포지션은 pnl_pct가 이미
    # TIME_STOP_MIN_RETURN_PCT를 넘어 있어서 자연히 이 조건에 안 걸림.
    days_elapsed = trading_days_elapsed(pos.get("entry_date_ny"), today_ny_date)
    if (
        days_elapsed is not None
        and days_elapsed >= TIME_STOP_TRADING_DAYS
        and pnl_pct < TIME_STOP_MIN_RETURN_PCT
    ):
        reasons.append(
            f"타임스탑(모멘텀 소멸, 진입 후 {days_elapsed}거래일 경과, 손익 {pnl_pct * 100:.1f}%)"
        )

    urgent = False
    drop = compute_recent_drop_pct(intraday)
    if drop is not None and drop <= CIRCUIT_BREAKER_PCT:
        reasons.append(f"직선급락(최근{CIRCUIT_BREAKER_LOOKBACK_MIN}분 {drop * 100:.1f}%)")
        urgent = True

    partial_profit_signal = (
        not pos.get("partial_profit_alerted")
        and pnl_pct >= PARTIAL_PROFIT_PCT
    )

    return {
        "symbol": symbol,
        "price": price,
        "pnl_pct": pnl_pct,
        "peak_price": peak_price,
        "exit_signal": len(reasons) > 0,
        "urgent": urgent,
        "reasons": reasons,
        "partial_profit_signal": partial_profit_signal,
    }


def main():
    positions = load_positions()
    entries, entry_time_utc = load_scanner2_entries()
    if entries:
        positions = open_new_positions(positions, entries, entry_time_utc)

    new_exits = []
    partial_profit_alerts = []
    errors = []
    now_utc = datetime.now(timezone.utc).isoformat()

    for pos in positions:
        if pos["status"] != "open":
            continue
        try:
            r = evaluate_position(pos)
        except Exception as e:
            errors.append({"symbol": pos["symbol"], "error": str(e)})
            continue

        if "error" in r:
            errors.append({"symbol": pos["symbol"], "error": r["error"]})
            continue

        pos["peak_price"] = r["peak_price"]  # 9/2 추가: 트레일링스탑 기준 최고가 매번 갱신

        if r["partial_profit_signal"]:
            pos["partial_profit_alerted"] = True
            partial_profit_alerts.append({
                "symbol": pos["symbol"],
                "price": r["price"],
                "pnl_pct": r["pnl_pct"],
            })

        if r["exit_signal"]:
            pos["status"] = "closed"
            pos["exit_price"] = r["price"]
            pos["exit_time_utc"] = now_utc
            pos["exit_reasons"] = r["reasons"]
            pos["pnl_pct"] = r["pnl_pct"]
            pos["urgent"] = r["urgent"]
            new_exits.append(dict(pos))

    open_count = sum(1 for p in positions if p["status"] == "open")

    output = {
        "updated_at_utc": now_utc,
        "open_position_count": open_count,
        "new_exits": new_exits,
        "partial_profit_alerts": partial_profit_alerts,
        "errors": errors[:10],
    }

    with open(POSITIONS_FILE, "w") as f:
        json.dump({"positions": positions}, f, indent=2, ensure_ascii=False)

    with open(RESULT_FILE, "w") as f:
        json.dump(output, f, indent=2, ensure_ascii=False)

    print(
        f"오픈 포지션: {open_count}건 / 이번 청산: {len(new_exits)}건 / "
        f"1차익절신호: {len(partial_profit_alerts)}건 / 에러: {len(errors)}건"
    )
    print(json.dumps(output, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
