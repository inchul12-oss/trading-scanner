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

from vcp_setup import evaluate_vcp, Params


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
                 commission_pct=0.0):
        self.__dict__.update(locals())
        del self.__dict__["self"]


# ────────────────────────── 백테스트 본체 ──────────────────────────
def backtest_symbol(symbol, bars, cfg: BTConfig, p: Params = None):
    """단일 종목 워크포워드 시뮬레이션. bars: open/high/low/close/volume 리스트 dict."""
    p = p or Params()
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
                trades.append(_finalize(pos, cfg))
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
                    pos = {"symbol": symbol, "entry_idx": i, "entry": fill,
                           "stop": order["stop"], "pivot": order["pivot"],
                           "setup_idx": order["setup_idx"],
                           "risk": fill - order["stop"],
                           "peak": h[i], "trough": l[i], "same_bar_stop": False}
                    order = None
                    # 같은 봉에서 손절선까지 닿았다면 그 봉에서 바로 청산된 것으로 처리
                    if l[i] <= pos["stop"]:
                        pos.update({"exit_idx": i, "exit": pos["stop"] * (1 - cfg.slippage_pct),
                                    "reason": "구조적손절(진입당일)", "same_bar_stop": True,
                                    "holding_days": 0})
                        trades.append(_finalize(pos, cfg))
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


def _finalize(pos, cfg):
    risk = pos["risk"] if pos["risk"] > 0 else 1e-9
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
    }


# ────────────────────────── 대조군: 단순 일봉 추세돌파 ──────────────────────────
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
                trades.append(_finalize(pos, cfg))
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
            pos = {"symbol": symbol, "entry_idx": i + 1, "entry": entry, "stop": stop,
                   "pivot": prior_high, "setup_idx": i, "risk": entry - stop,
                   "peak": h[i + 1], "trough": l[i + 1]}
    return trades


# ────────────────────────── 성과 집계 ──────────────────────────
def summarize(trades):
    if not trades:
        return {"n": 0}
    rs = [t["r"] for t in trades]
    wins = [x for x in rs if x > 0]
    losses = [x for x in rs if x <= 0]
    gp = sum(wins) or 0.0
    gl = abs(sum(losses)) or 1e-9
    avg_w = sum(wins) / len(wins) if wins else 0
    avg_l = sum(losses) / len(losses) if losses else 0
    wr = len(wins) / len(rs)
    return {
        "n": len(rs),
        "win_rate": round(wr, 3),
        "expectancy_r": round(sum(rs) / len(rs), 3),
        "profit_factor": round(gp / gl, 2),
        "avg_win_r": round(avg_w, 2), "avg_loss_r": round(avg_l, 2),
        "median_r": round(sorted(rs)[len(rs) // 2], 3),
        "max_r": round(max(rs), 2), "min_r": round(min(rs), 2),
        "avg_hold": round(sum(t["holding_days"] for t in trades) / len(trades), 1),
        "avg_giveback_r": round(sum(t["giveback_r"] for t in trades) / len(trades), 2),
        "total_ret_pct": round(sum(t["ret_pct"] for t in trades) * 100, 2),
    }
