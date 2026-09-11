"""
VCP 백테스트 엔진 — 라이브와 완전히 같은 탐지 함수(evaluate_vcp)를 사용한다.

핵심 원칙
1) 탐지는 vcp_setup.evaluate_vcp 하나만 쓴다. 백테스트 전용 탐지 로직을 따로 만들지 않는다.
   → "백테스트에선 됐는데 실전에선 다르게 동작"하는 문제가 구조적으로 불가능해진다.
2) 주문은 증권사에서 실제로 걸 수 있는 형태만 모델링한다(9/9 확인 결과 기준).
   - 매수: "가격이 T 이상이 되면 지정가 L로 매수"  → 스탑리밋
   - 손절: 절대 가격 지정, 자동 감시 → 장중 체결 모델링 가능
   - 익절: 절대 가격 지정, 손절과 OCO
3) 낙관적 가정을 금지한다. 애매하면 항상 불리한 쪽으로 체결시킨다.
   - 같은 봉에서 진입과 손절이 모두 닿으면 손절이 먼저 닿은 것으로 본다.
   - 갭으로 지정가를 넘겨 열리면 미체결(스킵)로 본다.
"""

from vcp_setup import evaluate_vcp, Params, _find_swing_lows


# ────────────────────────── 보조 계산 ──────────────────────────
def ema_series(vals, n):
    out, k, prev = [], 2 / (n + 1), None
    for v in vals:
        prev = v if prev is None else prev + k * (v - prev)
        out.append(prev)
    return out


def atr_series(high, low, close, n):
    out, trs = [], []
    for j in range(len(close)):
        tr = (high[j] - low[j]) if j == 0 else max(
            high[j] - low[j], abs(high[j] - close[j - 1]), abs(low[j] - close[j - 1]))
        trs.append(tr)
        out.append(sum(trs[-n:]) / n if len(trs) >= n else None)
    return out


# ────────────────────────── 설정 ──────────────────────────
class BTConfig:
    def __init__(self,
                 entry_mode="stop_buy",       # stop_buy | close_confirm | close_confirm_2day
                 exit_mode="ema10",           # ema10 | ema20 | atr_trail
                 atr_trail_mult=2.5,
                 order_valid_days=30,         # 조건주문 유효기간(증권사 30일 감시 가정)
                 early_fail_days=2,           # 돌파 후 N일 안에 종가가 피봇 아래면 조기청산
                 time_stop_days=25,           # 보유 N일 경과 + 조건 미달이면 청산
                 time_stop_min_r=1.0,         # 그때까지 +1R도 못 가면 청산
                 market_filter=None,          # None 또는 {'close':[...], 'sma':50} 지수 데이터
                 slippage_pct=0.0005,         # 체결 슬리피지(편도)
                 commission_pct=0.0,
                 # ── 9/11 추가: 손절폭 하한 ─────────────────────────────
                 # 배경: risk = entry - stop 이 0에 가까우면 R = 수익/risk 가 발산해서
                 # 기대값이 607만 같은 값으로 오염된다. 이전 코드는 risk<=0 을 1e-9 로
                 # 치환했는데, 그건 통계 문제가 아니라 "정의가 잘못된 거래"라 무효 처리해야 한다.
                 # 손절가를 억지로 넓히지 않고, 하한 미달이면 그냥 거래하지 않는다.
                 min_risk_pct=0.01,           # 진입가 대비 최소 손절폭 1%
                 min_risk_atr_mult=0.5,       # 또는 0.5 * ATR14, 둘 중 큰 값
                 max_risk_pct=0.05,           # 진입가 대비 최대 손절폭 5%
                 post_exit_horizons=(10, 20, 40)):
        self.__dict__.update(locals())
        del self.__dict__["self"]


# ────────────────────────── 손절폭 게이트 ──────────────────────────
# 반환: ("ok"|사유코드, risk)
#  invalid_risk      진입가 <= 손절가. 정의 자체가 틀린 거래.
#  risk_window_empty 하한 > 상한. 변동성이 너무 커서 통과 가능한 구간이 아예 없음.
#                    (예: ATR14가 주가의 12% → 하한 6% > 상한 5%)
#                    이건 "우리가 감당 못 하는 종목"이라 거르는 게 맞지만,
#                    조용히 사라지면 나중에 원인을 못 찾으므로 반드시 따로 센다.
#  risk_below_floor  손절폭이 하한보다 좁음 (스프레드만으로 털리는 자리)
#  risk_above_cap    손절폭이 상한보다 넓음
def risk_gate(entry, stop, atr_val, cfg):
    risk = entry - stop
    if risk <= 0:
        return "invalid_risk", risk
    floor = max(cfg.min_risk_pct * entry, cfg.min_risk_atr_mult * (atr_val or 0.0))
    cap = cfg.max_risk_pct * entry
    if floor > cap:
        return "risk_window_empty", risk
    if risk < floor:
        return "risk_below_floor", risk
    if risk > cap:
        return "risk_above_cap", risk
    return "ok", risk


def new_counters():
    return {"ok": 0, "invalid_risk": 0, "risk_window_empty": 0,
            "risk_below_floor": 0, "risk_above_cap": 0,
            "same_bar_ambiguous": 0, "entries": 0}


def _post_exit_mfe(entry, risk, bars, exit_idx, horizons):
    """청산 이후에도 주가가 얼마나 더 갔는지. 최초 진입가 기준 R로 환산.
    청산이 수익을 잘랐는지(추세이탈 건) / 손절이 너무 타이트했는지(손절 건) 판정용."""
    h, n = bars["high"], len(bars["high"])
    out = {}
    for H in horizons:
        s = exit_idx + 1
        if s >= n:
            out[H] = None
            continue
        out[H] = round((max(h[s:min(n, s + H)]) - entry) / risk, 3)
    return out


# ────────────────────────── 백테스트 본체 ──────────────────────────
def backtest_symbol(symbol, bars, cfg: BTConfig, p: Params = None, counters=None):
    """단일 종목 워크포워드 시뮬레이션. bars: open/high/low/close/volume 리스트 dict."""
    p = p or Params()
    if counters is None:
        counters = new_counters()
    o, h, l, c = bars["open"], bars["high"], bars["low"], bars["close"]
    n = len(c)

    ema10 = ema_series(c, 10)
    ema20 = ema_series(c, 20)
    atr14 = atr_series(h, l, c, 14)

    trades, order, pos = [], None, None
    start = p.max_base + p.atr_long + 6

    for i in range(start, n - 1):
        # ── 보유 중이면 청산 판정 ──────────────────────────────
        if pos:
            j = i
            exited = _check_exit(pos, j, bars, ema10, ema20, atr14, cfg)
            if exited:
                pos.update(exited)
                pos["holding_days"] = j - pos["entry_idx"]
                trades.append(_finalize(pos, cfg, bars))
                pos = None
            else:
                pos["peak"] = max(pos["peak"], h[j])
                pos["trough"] = min(pos["trough"], l[j])
            continue

        # ── 대기 주문이 있으면 체결 판정 ───────────────────────
        if order:
            if i > order["expire_idx"]:
                order = None
            else:
                fill = _check_fill(order, i, bars, cfg)
                if fill == "invalidated":
                    order = None
                elif fill is not None:
                    # ── 손절폭 게이트 (9/11 추가) ──────────────────────
                    verdict, risk = risk_gate(fill, order["stop"], atr14[i - 1], cfg)
                    counters[verdict] = counters.get(verdict, 0) + 1
                    if verdict != "ok":
                        order = None
                        continue
                    counters["entries"] += 1

                    # 같은 날 진입가와 손절가에 모두 닿았으면 일봉으로는 순서를 알 수 없다.
                    # 보수적으로 손절 처리하되, 이런 거래가 몇 %인지 반드시 센다.
                    # (시가 갭으로 열려 시가에 체결된 경우는 진입이 먼저인 게 확실하므로 제외)
                    ambiguous = (cfg.entry_mode == "stop_buy"
                                 and fill > o[i] * (1 + cfg.slippage_pct) * 1.0000001
                                 and l[i] <= order["stop"])
                    if ambiguous:
                        counters["same_bar_ambiguous"] += 1

                    pos = {"symbol": symbol, "entry_idx": i, "entry": fill,
                           "stop": order["stop"], "pivot": order["pivot"],
                           "setup_idx": order["setup_idx"],
                           "risk": risk,
                           "peak": h[i], "trough": l[i],
                           "same_bar_stop": False, "ambiguous": ambiguous}
                    order = None
                    # 같은 봉에서 손절선까지 닿았다면 그 봉에서 바로 청산된 것으로 처리
                    if l[i] <= pos["stop"]:
                        pos.update({"exit_idx": i, "exit": pos["stop"] * (1 - cfg.slippage_pct),
                                    "reason": "구조적손절(진입당일)", "same_bar_stop": True,
                                    "holding_days": 0})
                        trades.append(_finalize(pos, cfg, bars))
                        pos = None
                    continue

        # ── 신규 셋업 탐지 ─────────────────────────────────────
        if order is None and pos is None:
            if cfg.market_filter and not _market_ok(cfg.market_filter, i):
                continue
            r = evaluate_vcp(bars, i, p)
            # 9/11(판호 지적 반영): 종가가 이미 피봇 위인 셋업(late_breakout)은 탈락이 아니라 상태다.
            # 장중 스탑매수는 이미 놓친 돌파이므로 주문을 걸지 않고, 종가확인 방식은 그게 곧 신호다.
            if r.get("ready") and r.get("late_breakout") and cfg.entry_mode == "stop_buy":
                continue
            if r.get("ready"):
                order = {"setup_idx": i, "pivot": r["pivot"], "stop": r["structural_stop"],
                         "max_entry": r["max_entry"],
                         "trigger": r["pivot"] * 1.003,
                         "expire_idx": i + cfg.order_valid_days,
                         "armed_mode": cfg.entry_mode, "confirm_idx": None}
    return trades


def _market_ok(mf, i):
    cl, n = mf["close"], mf["sma"]
    if i + 1 < n:
        return True
    return cl[i] > sum(cl[i - n + 1:i + 1]) / n


def _check_fill(order, i, bars, cfg):
    """이 봉에서 매수 주문이 체결되는지. 체결가 반환, 미체결이면 None, 셋업 무효면 'invalidated'."""
    o, h, l, c = bars["open"], bars["high"], bars["low"], bars["close"]
    pivot, stop, cap = order["pivot"], order["stop"], order["max_entry"]

    # 셋업 무효화: 종가가 구조적 손절선 아래로 마감하면 그 셋업은 죽은 것으로 본다
    if c[i] < stop:
        return "invalidated"

    if cfg.entry_mode == "stop_buy":
        trig = order["trigger"]
        if h[i] < trig:
            return None
        # 9/11 수정(낙관 편향 제거): 같은 봉에서 진입가와 손절가가 모두 닿은 경우를
        # 예전엔 "거래 없음"으로 처리했는데, 이건 손실 한 건을 통째로 지워버리는 낙관적 처리였다.
        # 실제로는 손절 주문이 진입 직후 활성화되므로 "진입 후 손절"(-1R)이 맞다.
        # 여기서는 정상 체결시키고, 호출부에서 같은 봉에 즉시 청산 판정을 돌린다.
        px = max(o[i], trig)          # 갭이면 시가, 아니면 트리거 가격
        if px > cap:
            return None               # 지정가 상한 초과 → 미체결
        return px * (1 + cfg.slippage_pct)

    # 종가 확인형: 돌파 확인은 전일, 체결은 다음날 시가
    if order["confirm_idx"] is None:
        if c[i] > pivot:
            need = 2 if cfg.entry_mode == "close_confirm_2day" else 1
            order["confirm_count"] = order.get("confirm_count", 0) + 1
            if order["confirm_count"] >= need:
                order["confirm_idx"] = i
        else:
            order["confirm_count"] = 0
        return None

    px = o[i]
    if px > cap:
        order["confirm_idx"] = None   # 갭으로 놓침, 다시 대기
        order["confirm_count"] = 0
        return None
    return px * (1 + cfg.slippage_pct)


def _check_exit(pos, j, bars, ema10, ema20, atr14, cfg):
    """청산 판정. 우선순위: 장중 손절(자동주문) → 조기실패 → 추세이탈 → 타임스탑."""
    o, h, l, c = bars["open"], bars["high"], bars["low"], bars["close"]

    # 1) 구조적 손절 — 증권사 자동 손절주문이 실제로 걸려 있으므로 장중 체결로 모델링.
    #    단 갭하락이면 손절가가 아니라 시가에 체결된다(불리한 쪽).
    if l[j] <= pos["stop"]:
        px = min(o[j], pos["stop"]) if o[j] <= pos["stop"] else pos["stop"]
        return {"exit_idx": j, "exit": px * (1 - cfg.slippage_pct), "reason": "구조적손절"}

    # 2) 조기실패 — 돌파 후 N일 안에 종가가 피봇 아래로 복귀하면 다음날 시가 청산
    if j - pos["entry_idx"] <= cfg.early_fail_days and c[j] < pos["pivot"]:
        if j + 1 < len(c):
            return {"exit_idx": j + 1, "exit": o[j + 1] * (1 - cfg.slippage_pct),
                    "reason": "조기실패(피봇하회)"}

    # 3) 추세 이탈 — 종가 기준 판정, 다음날 시가 실행
    trend_break = False
    if cfg.exit_mode == "ema10":
        trend_break = c[j] < ema10[j]
    elif cfg.exit_mode == "ema20":
        trend_break = c[j] < ema20[j]
    elif cfg.exit_mode == "atr_trail" and atr14[j]:
        trend_break = c[j] < pos["peak"] - cfg.atr_trail_mult * atr14[j]
    if trend_break and j + 1 < len(c):
        return {"exit_idx": j + 1, "exit": o[j + 1] * (1 - cfg.slippage_pct),
                "reason": f"추세이탈({cfg.exit_mode})"}

    # 4) 타임스탑 — 오래 들고 있는데 못 가면 정리
    held = j - pos["entry_idx"]
    if held >= cfg.time_stop_days and pos["risk"] > 0:
        best_r = (pos["peak"] - pos["entry"]) / pos["risk"]
        if best_r < cfg.time_stop_min_r and j + 1 < len(c):
            return {"exit_idx": j + 1, "exit": o[j + 1] * (1 - cfg.slippage_pct),
                    "reason": "타임스탑"}
    return None


def _finalize(pos, cfg, bars=None):
    # 9/11: epsilon 치환 금지. 여기 도달하기 전에 risk_gate 가 걸러내므로
    # risk<=0 이 들어오면 그건 버그다. 조용히 1e-9로 덮지 않고 터뜨린다.
    risk = pos["risk"]
    if risk <= 0:
        raise ValueError(f"risk<=0 이 _finalize 까지 도달함: {pos['symbol']} {pos['entry_idx']}")
    ret_pct = pos["exit"] / pos["entry"] - 1 - cfg.commission_pct
    r_mult = (pos["exit"] - pos["entry"]) / risk
    peak_r = (pos["peak"] - pos["entry"]) / risk
    mae_r = (pos["trough"] - pos["entry"]) / risk
    return {
        "symbol": pos["symbol"], "setup_idx": pos["setup_idx"],
        "entry_idx": pos["entry_idx"], "exit_idx": pos["exit_idx"],
        "entry": round(pos["entry"], 4), "exit": round(pos["exit"], 4),
        "stop": round(pos["stop"], 4), "pivot": round(pos["pivot"], 4),
        "risk_pct": round(risk / pos["entry"], 4),
        "ret_pct": round(ret_pct, 4), "r": round(r_mult, 3),
        "peak_r": round(peak_r, 3), "mae_r": round(mae_r, 3),
        "giveback_r": round(peak_r - r_mult, 3),
        "holding_days": pos.get("holding_days", 0), "reason": pos["reason"],
        "ambiguous": bool(pos.get("ambiguous", False)),
        "entry_date": (bars["date"][pos["entry_idx"]]
                       if bars and "date" in bars else None),
        "exit_date": (bars["date"][pos["exit_idx"]]
                      if bars and "date" in bars else None),
        # 청산 이후에도 더 갔는가. 진입가 기준 R.
        "post_mfe": (_post_exit_mfe(pos["entry"], risk, bars, pos["exit_idx"],
                                    cfg.post_exit_horizons) if bars else None),
    }


# ────────────────────────── 대조군: 단순 일봉 추세돌파 ──────────────────────────
def backtest_matched_control(symbol, bars, cfg: BTConfig, p: Params = None,
                             lookback=20, sma_trend=50, counters=None):
    """9/11 추가. 판호 지적 반영한 '공정 비교용' 대조군.

    기존 대조군은 손절이 진입가-2*ATR14 라서, VCP(직전저점 기준 + 5% 상한)와
    R의 분모 정의가 서로 달랐다. 분모가 다르면 R끼리 비교 자체가 성립하지 않는다.
    이 버전은 진입 신호(20일 신고가 돌파)만 남기고 손절·리스크제한·슬리피지·청산을
    VCP와 완전히 동일하게 맞춘다. → VCP 셋업 필터만 ON/OFF 한 진짜 비교가 된다."""
    p = p or Params()
    if counters is None:
        counters = new_counters()
    o, h, l, c = bars["open"], bars["high"], bars["low"], bars["close"]
    n = len(c)
    atr14 = atr_series(h, l, c, 14)
    atr_s = atr_series(h, l, c, p.atr_short)
    ema10, ema20 = ema_series(c, 10), ema_series(c, 20)
    trades, pos = [], None

    for i in range(max(lookback, sma_trend) + 2, n - 1):
        if pos:
            ex = _check_exit(pos, i, bars, ema10, ema20, atr14, cfg)
            if ex:
                pos.update(ex)
                pos["holding_days"] = i - pos["entry_idx"]
                trades.append(_finalize(pos, cfg, bars))
                pos = None
            else:
                pos["peak"] = max(pos["peak"], h[i])
                pos["trough"] = min(pos["trough"], l[i])
            continue
        if cfg.market_filter and not _market_ok(cfg.market_filter, i):
            continue
        sma = sum(c[i - sma_trend + 1:i + 1]) / sma_trend
        prior_high = max(h[i - lookback:i])
        if not (c[i] > prior_high and c[i] > sma):
            continue

        # VCP와 동일한 손절 알고리즘.
        # VCP는 "마지막 수축 구간의 스윙 저점"을 쓴다. 대조군에는 수축 구간이 없으므로
        # 같은 _find_swing_lows 로 최근 스윙 저점을 찾아 쓴다(없으면 구간 최저점).
        # 단순히 20일 최저점을 쓰면 손절폭이 구조적으로 훨씬 넓어져 5% 상한에 거의 다 걸린다.
        sw = _find_swing_lows(l, i - lookback, i, p.swing_k)
        low_ref = l[sw[-1]] if sw else min(l[i - lookback + 1:i + 1])
        buf = max(p.stop_atr_mult * (atr_s[i] or c[i] * 0.01), c[i] * p.stop_min_pct)
        stop = low_ref - buf
        entry = o[i + 1] * (1 + cfg.slippage_pct)

        verdict, risk = risk_gate(entry, stop, atr14[i], cfg)
        counters[verdict] = counters.get(verdict, 0) + 1
        if verdict != "ok":
            continue
        counters["entries"] += 1
        pos = {"symbol": symbol, "entry_idx": i + 1, "entry": entry, "stop": stop,
               "pivot": prior_high, "setup_idx": i, "risk": risk,
               "peak": h[i + 1], "trough": l[i + 1], "ambiguous": False}
    return trades


def backtest_simple_breakout(symbol, bars, cfg: BTConfig, lookback=20, sma_trend=50):
    """판호 제안 대조군. '종가가 N일 신고가 + 추세필터' 뿐인 다섯 줄짜리 전략.
    복잡한 VCP가 이걸 못 이기면 VCP의 복잡도를 유지할 이유가 없다."""
    o, h, l, c = bars["open"], bars["high"], bars["low"], bars["close"]
    n = len(c)
    atr14 = atr_series(h, l, c, 14)
    ema10, ema20 = ema_series(c, 10), ema_series(c, 20)
    trades, pos = [], None

    for i in range(max(lookback, sma_trend) + 2, n - 1):
        if pos:
            ex = _check_exit(pos, i, bars, ema10, ema20, atr14, cfg)
            if ex:
                pos.update(ex)
                pos["holding_days"] = i - pos["entry_idx"]
                trades.append(_finalize(pos, cfg, bars))
                pos = None
            else:
                pos["peak"] = max(pos["peak"], h[i])
                pos["trough"] = min(pos["trough"], l[i])
            continue
        if cfg.market_filter and not _market_ok(cfg.market_filter, i):
            continue
        sma = sum(c[i - sma_trend + 1:i + 1]) / sma_trend
        prior_high = max(h[i - lookback:i])
        if c[i] > prior_high and c[i] > sma:
            entry = o[i + 1] * (1 + cfg.slippage_pct)
            stop = entry - 2.0 * (atr14[i] or entry * 0.03)
            if entry - stop <= 0:
                continue
            pos = {"symbol": symbol, "entry_idx": i + 1, "entry": entry, "stop": stop,
                   "pivot": prior_high, "setup_idx": i, "risk": entry - stop,
                   "peak": h[i + 1], "trough": l[i + 1], "ambiguous": False}
    return trades


# ────────────────────────── 성과 집계 ──────────────────────────
def summarize(trades):
    if not trades:
        return {"n": 0}
    rs = [t["r"] for t in trades]
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x <= 0]
    gp = sum(wins) or 0.0
    # 손실이 하나도 없으면 PF는 무한대다. 1e-9로 나눠서 200억 같은 숫자를 만들면
    # 나중에 그게 진짜 성과인 줄 알고 읽게 된다. 정의되지 않는 건 None으로 남긴다.
    gl = abs(sum(losses))
    avg_w = sum(wins) / len(wins) if wins else 0
    avg_l = sum(losses) / len(losses) if losses else 0
    wr = len(wins) / len(rs)
    return {
        "n": len(rs),
        "win_rate": round(wr, 3),
        "expectancy_r": round(sum(rs) / len(rs), 3),
        "profit_factor": round(gp / gl, 2) if gl > 0 else None,
        "avg_win_r": round(avg_w, 2), "avg_loss_r": round(avg_l, 2),
        "median_r": round(sorted(rs)[len(rs) // 2], 3),
        "max_r": round(max(rs), 2), "min_r": round(min(rs), 2),
        "avg_hold": round(sum(t["holding_days"] for t in trades) / len(trades), 1),
        "avg_giveback_r": round(sum(t["giveback_r"] for t in trades) / len(trades), 2),
        "total_ret_pct": round(sum(t["ret_pct"] for t in trades) * 100, 2),
        # 손절 정의가 서로 다른 전략끼리는 R 비교가 성립하지 않는다.
        # 거래당 평균 %수익률은 단위가 같아서 전략 간 비교가 가능하다.
        "avg_ret_pct": round(sum(t["ret_pct"] for t in trades) / len(trades) * 100, 3),
        "ambiguous_pct": round(
            sum(1 for t in trades if t.get("ambiguous")) / len(trades) * 100, 2),
        **portfolio_stats(trades),
        **_post_exit_summary(trades),
    }


def portfolio_stats(trades):
    """청산일 기준으로 R을 누적해서 포트폴리오 관점의 낙폭을 구한다.
    거래별 min_r(최악의 한 건)과 달리, 연달아 지는 구간의 크기를 본다.
    한 번에 한 종목만 들고 있다고 가정한 단순 누적이라 실제 계좌와 정확히 같진 않다.
    날짜가 없으면(합성데이터 등) None."""
    ts = [t for t in trades if t.get("exit_date")]
    if not ts:
        return {}
    ts.sort(key=lambda t: t["exit_date"])
    eq = peak = 0.0
    mdd = 0.0
    for t in ts:
        eq += t["r"]
        peak = max(peak, eq)
        mdd = min(mdd, eq - peak)
    return {"total_r": round(eq, 1), "max_drawdown_r": round(mdd, 1),
            "first_exit": ts[0]["exit_date"], "last_exit": ts[-1]["exit_date"]}


def _post_exit_summary(trades):
    """청산 후 MFE를 두 갈래로 완전히 분리해서 집계한다.

    섞으면 안 되는 이유: 손절당한 거래가 그 후 크게 올랐다고 해서 "청산이 문제"는 아니다.
    손절은 애초에 지켜야 하는 것이라 그 수익은 우리가 가질 수 없었던 것이다.
      trend_exit  = 추세이탈로 익절하고 나온 거래 → 그 후에도 더 갔으면 '청산이 수익을 자른' 증거
      stopped     = 손절당한 거래              → 그 후 크게 올랐으면 '손절이 너무 타이트' 증거
    """
    out = {}
    groups = {
        "post_trend": [t for t in trades
                       if t["reason"].startswith("추세이탈") and t["r"] > 0],
        "post_stop": [t for t in trades if t["reason"].startswith("구조적손절")],
    }
    for name, ts in groups.items():
        out[f"{name}_n"] = len(ts)
        if not ts:
            continue
        for H in (10, 20, 40):
            vals = [t["post_mfe"][H] for t in ts
                    if t.get("post_mfe") and t["post_mfe"].get(H) is not None]
            if not vals:
                continue
            out[f"{name}_mfe{H}_med"] = round(sorted(vals)[len(vals) // 2], 2)
            out[f"{name}_mfe{H}_avg"] = round(sum(vals) / len(vals), 2)
        # 청산 시점보다 얼마나 더 갔는지 (추세이탈 건만 의미 있음)
        if name == "post_trend":
            for H in (10, 20, 40):
                extra = [t["post_mfe"][H] - t["r"] for t in ts
                         if t.get("post_mfe") and t["post_mfe"].get(H) is not None]
                if extra:
                    out[f"post_trend_extra{H}_med"] = round(
                        sorted(extra)[len(extra) // 2], 2)
                    out[f"post_trend_extra{H}_gt1R_pct"] = round(
                        sum(1 for x in extra if x >= 1.0) / len(extra) * 100, 1)
    return out
