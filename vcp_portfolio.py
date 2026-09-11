"""
청산 규칙 확정용 검증 도구 (9/11, 판호 자문 반영).

왜 필요한가 — 9/11 청산 비교에서 드러난 문제 3개:

1) 청산 규칙마다 거래 수가 달랐다(1,621 ~ 1,958건).
   한 종목은 한 번에 하나만 보유하므로, 빨리 파는 규칙은 그 종목에서 다음 신호를
   또 잡고 늦게 파는 규칙은 놓친다. 즉 청산 규칙이 "거래 목록"까지 바꿔버려서
   순수한 청산 비교가 아니었다.
   → collect_entries()로 진입 목록을 한 번만 만들고, 같은 목록에 청산만 갈아끼운다.

2) 검증 구간(2021~2026)은 상승 구간을 다수 포함한다. 상승장에서는 오래 들고 있는
   규칙이 청산 품질과 무관하게 유리하게 나온다.
   → 종목별 베타를 진입 전 126일로 추정하고, 보유기간 동안 시장이 올려준 몫을
     빼낸 초과수익(alpha)으로 다시 비교한다.

3) 기존 최대낙폭은 "한 번에 한 종목만 보유" 가정의 근사였다. 보유기간이 3배 다르면
   자본 점유와 동시보유 수가 달라져 비교가 성립하지 않는다.
   → 5슬롯 포트폴리오를 일별 평가손익까지 포함해 시뮬레이션하고 MAR로 비교한다.
   → 최대낙폭은 5년에 한 번 나온 단일 실현값이라 매우 불안정하므로,
     3개월 블록 부트스트랩으로 분포를 구한다. 점추정 하나로 고르지 않는다.
"""

import random

from vcp_setup import Params, _find_swing_lows
from vcp_backtest import (BTConfig, ema_series, atr_series, risk_gate,
                          new_counters, _check_exit, _check_partial, _finalize)


# ───────────────────── 1) 진입 목록 고정 ─────────────────────
def collect_entries(symbol, bars, cfg: BTConfig, p: Params = None,
                    lookback=20, sma_trend=50, counters=None):
    """매칭 대조군의 진입 신호를 '보유 여부와 무관하게' 전부 수집한다.

    보유 제약을 일부러 빼는 이유: 여기서 만드는 목록은 실제 매매 계획이 아니라
    청산 규칙들을 같은 조건에서 비교하기 위한 고정 표본이다. 실제 동시보유 제약은
    뒤의 포트폴리오 시뮬레이션에서 슬롯으로 따로 모델링한다.
    """
    p = p or Params()
    if counters is None:
        counters = new_counters()
    o, h, l, c = bars["open"], bars["high"], bars["low"], bars["close"]
    n = len(c)
    atr14 = atr_series(h, l, c, 14)
    atr_s = atr_series(h, l, c, p.atr_short)
    out = []
    for i in range(max(lookback, sma_trend) + 2, n - 1):
        sma = sum(c[i - sma_trend + 1:i + 1]) / sma_trend
        prior_high = max(h[i - lookback:i])
        if not (c[i] > prior_high and c[i] > sma):
            continue
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
        out.append({"symbol": symbol, "setup_idx": i, "entry_idx": i + 1,
                    "entry": entry, "stop": stop, "pivot": prior_high, "risk": risk})
    return out


# ───────────────────── 2) 같은 진입에 청산만 갈아끼우기 ─────────────────────
def run_exit(ev, bars, cfg: BTConfig, p: Params = None):
    """진입 이벤트 하나에 청산 규칙 하나를 적용한다. 일별 R 경로도 같이 남긴다.
    r_path는 포트폴리오 일별 평가손익 계산에 쓴다(미실현 손익 포함 MDD)."""
    o, h, l, c = bars["open"], bars["high"], bars["low"], bars["close"]
    n = len(c)
    ema10, ema20, ema50 = ema_series(c, 10), ema_series(c, 20), ema_series(c, 50)
    atr14 = atr_series(h, l, c, 14)
    i0 = ev["entry_idx"]
    pos = dict(ev)
    pos.update({"peak": h[i0], "trough": l[i0], "ambiguous": False})

    # 진입 당일 손절 도달 → 그 봉에서 청산
    if l[i0] <= pos["stop"]:
        pos.update({"exit_idx": i0, "exit": pos["stop"] * (1 - cfg.slippage_pct),
                    "reason": "구조적손절(진입당일)", "holding_days": 0})
        t = _finalize(pos, cfg, bars)
        t["r_path"] = [(bars["date"][i0], t["r"])] if "date" in bars else []
        return t

    r_path = []
    for j in range(i0 + 1, n):
        ex = _check_exit(pos, j, bars, ema10, ema20, ema50, atr14, cfg)
        _check_partial(pos, j, bars, cfg, ex)
        if "date" in bars:
            r_path.append((bars["date"][j], (c[j] - pos["entry"]) / pos["risk"]))
        if ex:
            pos.update(ex)
            pos["holding_days"] = j - i0
            t = _finalize(pos, cfg, bars)
            if r_path:
                r_path[-1] = (r_path[-1][0], t["r"])   # 마지막 날은 실현 R로 교체
            t["r_path"] = r_path
            return t
        pos["peak"] = max(pos["peak"], h[j])
        pos["trough"] = min(pos["trough"], l[j])
    return None      # 데이터 끝까지 청산되지 않음 → 표본에서 제외


# ───────────────────── 3) 베타 보정 ─────────────────────
def estimate_beta(sym_close, bench_by_date, dates, i_end, window=126):
    """진입 직전 window일로 종목 베타를 추정한다. 미래를 보지 않는다."""
    i0 = max(1, i_end - window)
    xs, ys = [], []
    for j in range(i0, i_end):
        d0, d1 = dates[j - 1], dates[j]
        b0, b1 = bench_by_date.get(d0), bench_by_date.get(d1)
        if not b0 or not b1 or sym_close[j - 1] <= 0:
            continue
        xs.append(b1 / b0 - 1)
        ys.append(sym_close[j] / sym_close[j - 1] - 1)
    if len(xs) < 40:
        return None
    mx = sum(xs) / len(xs)
    my = sum(ys) / len(ys)
    cov = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    var = sum((x - mx) ** 2 for x in xs)
    if var <= 0:
        return None
    b = cov / var
    return max(-1.0, min(4.0, b))      # 추정 폭주 방지


def add_alpha(trade, bars, bench_by_date, beta):
    """보유기간 동안 시장이 올려준 몫을 빼고 남은 초과수익(alpha)을 붙인다."""
    if beta is None or "date" not in bars:
        return
    d_in = bars["date"][trade["entry_idx"]]
    d_out = bars["date"][trade["exit_idx"]]
    b_in, b_out = bench_by_date.get(d_in), bench_by_date.get(d_out)
    if not b_in or not b_out:
        return
    mkt = b_out / b_in - 1
    trade["beta"] = round(beta, 3)
    trade["mkt_ret_pct"] = round(mkt, 4)
    trade["alpha_pct"] = round(trade["ret_pct"] - beta * mkt, 4)
    rp = trade.get("risk_pct") or 0.0
    if rp > 0:
        trade["alpha_r"] = round(trade["alpha_pct"] / rp, 3)


# ───────────────────── 4) 5슬롯 포트폴리오 시뮬레이션 ─────────────────────
def simulate_portfolio(trades, slots=5, risk_frac=0.01, all_dates=None):
    """실제 계좌 곡선을 그린다.
      - 시간순 진입, 최대 slots개 동시보유, 꽉 차면 신호 포기(선착순 — 규칙 사전 고정)
      - 진입 시점 자본의 risk_frac을 그 거래의 리스크로 배정
      - 매일 미실현 손익까지 반영해 자본을 평가 → 최대낙폭이 실제에 가깝다
    반환: CAGR, MDD, MAR, 체결/포기 건수, 최대 동시보유
    """
    ts = [t for t in trades if t.get("entry_date") and t.get("exit_date") and t.get("r_path")]
    if not ts:
        return {}
    ts.sort(key=lambda t: (t["entry_date"], t["symbol"]))
    if all_dates is None:
        s = set()
        for t in ts:
            s.update(d for d, _ in t["r_path"])
            s.add(t["entry_date"])
        all_dates = sorted(s)
    date_pos = {d: k for k, d in enumerate(all_dates)}

    by_entry = {}
    for t in ts:
        by_entry.setdefault(t["entry_date"], []).append(t)

    equity = 1.0
    peak = 1.0
    mdd = 0.0
    open_pos = []        # {risk_amt, path:{date->r}, exit_date, last_r}
    taken = skipped = 0
    max_open = 0
    curve = []

    for d in all_dates:
        # 청산 처리
        still = []
        for p_ in open_pos:
            if p_["exit_date"] <= d:
                equity += p_["risk_amt"] * p_["final_r"]
            else:
                still.append(p_)
        open_pos = still
        # 신규 진입
        for t in by_entry.get(d, []):
            if len(open_pos) >= slots:
                skipped += 1
                continue
            taken += 1
            open_pos.append({"risk_amt": equity * risk_frac,
                             "path": dict(t["r_path"]),
                             "exit_date": t["exit_date"],
                             "final_r": t["r"]})
        max_open = max(max_open, len(open_pos))
        # 일별 평가자본 (미실현 포함)
        unreal = 0.0
        for p_ in open_pos:
            r = p_["path"].get(d)
            if r is not None:
                p_["last_r"] = r
            unreal += p_["risk_amt"] * p_.get("last_r", 0.0)
        eq = equity + unreal
        peak = max(peak, eq)
        if peak > 0:
            mdd = min(mdd, eq / peak - 1)
        curve.append(eq)

    years = max(len(all_dates) / 252.0, 0.25)
    final = curve[-1] if curve else 1.0
    cagr = (final ** (1 / years) - 1) if final > 0 else -1.0
    return {"cagr": round(cagr, 4), "mdd": round(mdd, 4),
            "mar": round(cagr / abs(mdd), 2) if mdd < -1e-9 else None,
            "final_equity": round(final, 3),
            "taken": taken, "skipped": skipped, "max_open": max_open,
            "years": round(years, 2)}


# ───────────────────── 5) 블록 부트스트랩 ─────────────────────
def bootstrap_mar(trades, all_dates, slots=5, risk_frac=0.01,
                  block_days=63, iters=200, seed=7):
    """최대낙폭은 5년에 한 번 나온 단일 실현값이라 매우 불안정하다.
    3개월(63거래일) 블록을 복원추출해 5년 길이를 다시 만들고, 그때마다
    포트폴리오를 다시 돌려 MAR 분포를 구한다.
    한계: 블록 경계를 넘는 거래는 경계에서 강제 청산된다(그 시점 평가 R로).
    이 왜곡은 모든 청산 규칙에 동일하게 적용되므로 비교는 유지된다."""
    rnd = random.Random(seed)
    blocks = [all_dates[k:k + block_days] for k in range(0, len(all_dates), block_days)]
    blocks = [b for b in blocks if len(b) >= block_days // 2]
    if len(blocks) < 4:
        return {}
    by_date = {}
    for t in trades:
        if t.get("entry_date") and t.get("r_path"):
            by_date.setdefault(t["entry_date"], []).append(t)

    mars, cagrs, mdds = [], [], []
    for _ in range(iters):
        picked = [blocks[rnd.randrange(len(blocks))] for _ in range(len(blocks))]
        seq, remap = [], {}
        for blk in picked:
            for d in blk:
                nd = f"{len(seq):06d}"
                seq.append(nd)
                remap.setdefault(d, []).append(nd)
        # 블록 안에서만 거래를 재구성
        synth = []
        idx = 0
        for blk in picked:
            blkset = {d: f"{idx + k:06d}" for k, d in enumerate(blk)}
            idx += len(blk)
            for d in blk:
                for t in by_date.get(d, []):
                    path = [(blkset[dd], rr) for dd, rr in t["r_path"] if dd in blkset]
                    if not path:
                        continue
                    synth.append({"symbol": t["symbol"], "entry_date": blkset[d],
                                  "exit_date": path[-1][0], "r": path[-1][1],
                                  "r_path": path})
        res = simulate_portfolio(synth, slots, risk_frac, all_dates=seq)
        if res.get("mar") is not None:
            mars.append(res["mar"]); cagrs.append(res["cagr"]); mdds.append(res["mdd"])
    if not mars:
        return {}
    def pct(v, q):
        v = sorted(v); return round(v[min(len(v) - 1, int(q * len(v)))], 3)
    return {"n_iter": len(mars),
            "mar_p05": pct(mars, 0.05), "mar_med": pct(mars, 0.5), "mar_p95": pct(mars, 0.95),
            "cagr_med": pct(cagrs, 0.5), "mdd_med": pct(mdds, 0.5), "mdd_p05": pct(mdds, 0.05)}


# ───────────────────── 6) VCP 지표 원값 추출 ─────────────────────
# 왜 evaluate_vcp를 그대로 못 쓰나: 그 함수는 조건을 AND로 엮어서 하나라도 걸리면
# 즉시 return 한다. 그래서 대부분의 봉에서는 뒤쪽 지표값이 아예 계산되지 않는다.
# 여기서는 "통과/탈락" 판정 없이 각 지표의 원값만 독립적으로 뽑는다.
# 값이 정의되지 않으면 None을 넣고, 뒤의 분위 분석에서 그 건만 제외한다.
from vcp_setup import _atr        # noqa: E402


def extract_features(bars, i, p: Params = None):
    p = p or Params()
    high, low, close, vol = bars["high"], bars["low"], bars["close"], bars["volume"]
    c = close[i]
    f = {}
    if i < 60 or c <= 0:
        return f

    # 추세 위치
    if i + 1 >= p.sma_fast:
        sma50 = sum(close[i - p.sma_fast + 1:i + 1]) / p.sma_fast
        f["px_over_sma50"] = c / sma50 if sma50 else None
    if i + 1 >= p.sma_slow:
        sma150 = sum(close[i - p.sma_slow + 1:i + 1]) / p.sma_slow
        f["px_over_sma150"] = c / sma150 if sma150 else None
    if i + 1 >= p.high_52w_lookback:
        h52 = max(high[i - p.high_52w_lookback + 1:i + 1])
        f["pct_of_52w_high"] = c / h52 if h52 else None

    # 베이스
    lo_idx = i - p.max_base + 1
    if lo_idx < 0:
        return f
    seg = high[lo_idx:i + 1]
    b_idx = lo_idx + seg.index(max(seg))
    anchor = high[b_idx]
    f["base_len"] = i - b_idx

    adv_lo = max(0, b_idx - p.prior_advance_lookback)
    if adv_lo < b_idx:
        pl = min(low[adv_lo:b_idx])
        f["prior_advance"] = anchor / pl - 1 if pl else None

    # 수축 구조
    swings = _find_swing_lows(low, b_idx, i, p.swing_k)
    depths, kept, run = [], [], anchor
    for j in swings:
        run = max(run, max(high[b_idx:j + 1]))
        d = (run - low[j]) / run if run else 0
        if d >= p.min_contraction_depth:
            depths.append(d)
            kept.append(j)
    f["n_contractions"] = len(depths)
    if depths:
        f["depth_first"] = depths[0]
        f["depth_final"] = depths[-1]
        if depths[0] > 0:
            f["depth_final_over_first"] = depths[-1] / depths[0]
        if len(depths) >= 2:
            ratios = [depths[k + 1] / depths[k] for k in range(len(depths) - 1)]
            f["contraction_ratio_max"] = max(ratios)

    # 타이트함
    f["range10"] = (max(high[i - 9:i + 1]) - min(low[i - 9:i + 1])) / c
    f["range5"] = (max(high[i - 4:i + 1]) - min(low[i - 4:i + 1])) / c

    # 변동성 수축 (NATR 중앙값 비교)
    def _natr_med(center, span):
        vals = []
        for j in range(max(0, center - span + 1), center + 1):
            a = _atr(high, low, close, p.atr_short, j)
            if a and close[j]:
                vals.append(a / close[j])
        if not vals:
            return None
        vals.sort()
        return vals[len(vals) // 2]

    nb, nn = _natr_med(b_idx, p.natr_median_span_base), _natr_med(i, p.natr_median_span_now)
    f["natr_now"] = nn
    if nb and nn:
        f["atr_contraction"] = nn / nb

    # 거래량 고갈
    lo_v = i - p.vol_short - p.vol_long + 1
    if lo_v >= 0:
        v_rec = sum(vol[i - p.vol_short + 1:i + 1]) / p.vol_short
        v_base = sum(vol[lo_v:i - p.vol_short + 1]) / p.vol_long
        if v_base > 0:
            f["vol_ratio"] = v_rec / v_base
    return f


# ───────────────────── 7) 분위 분석 (train 경계 → test 적용) ─────────────────────
def quantile_study(trades, feature_names, target="alpha_r",
                   train_frac=0.6, buckets=5, min_per_bucket=30):
    """판호 권고 방식:
      - 전반 train_frac 기간에서 분위 경계를 정한다
      - 그 경계를 그대로 후반(test)에 적용한다 (test를 다시 분위분할하지 않는다.
        다시 나누면 시장 분포 변화까지 정규화돼서 임계값 안정성을 볼 수 없다)
      - train에서 단조성이 보이고 test에서 재현되는 지표만 채택
    """
    ts = [t for t in trades if t.get("entry_date") and t.get(target) is not None]
    if not ts:
        return {}
    ts.sort(key=lambda t: t["entry_date"])
    cut = int(len(ts) * train_frac)
    train, test = ts[:cut], ts[cut:]
    out = {"n_train": len(train), "n_test": len(test),
           "split_date": test[0]["entry_date"] if test else None, "features": {}}

    for fn in feature_names:
        tv = sorted(t["feat"][fn] for t in train
                    if t.get("feat", {}).get(fn) is not None)
        if len(tv) < buckets * min_per_bucket:
            continue
        edges = [tv[int(len(tv) * k / buckets)] for k in range(1, buckets)]

        def bucketize(rows):
            bs = [[] for _ in range(buckets)]
            for t in rows:
                v = t.get("feat", {}).get(fn)
                if v is None:
                    continue
                k = 0
                while k < buckets - 1 and v > edges[k]:
                    k += 1
                bs[k].append(t[target])
            return bs

        def stat(bs):
            o = []
            for b in bs:
                if len(b) < min_per_bucket:
                    o.append(None)
                    continue
                b2 = sorted(b)
                o.append({"n": len(b), "mean": round(sum(b) / len(b), 4),
                          "med": round(b2[len(b2) // 2], 4)})
            return o

        st_tr, st_te = stat(bucketize(train)), stat(bucketize(test))

        def mono(st):
            """분위 번호와 평균 사이의 스피어만 상관. +1/-1에 가까울수록 계단."""
            pts = [(k, s["mean"]) for k, s in enumerate(st) if s]
            if len(pts) < 4:
                return None
            n = len(pts)
            rk = {v: r for r, (_, v) in enumerate(sorted(pts, key=lambda x: x[1]))}
            d2 = sum((k - rk[v]) ** 2 for k, v in pts)
            return round(1 - 6 * d2 / (n * (n * n - 1)), 3)

        out["features"][fn] = {"edges": [round(e, 5) for e in edges],
                               "train": st_tr, "test": st_te,
                               "mono_train": mono(st_tr), "mono_test": mono(st_te)}
    return out


# ───────────────────── 8) 비용 차감 (판호 권고) ─────────────────────
# 백테스트는 이미 진입/청산가에 편도 0.05% 슬리피지를 반영하고 있다.
# 여기서 추가로 빼는 건 모델에 없던 비용(호가 스프레드, 체결 충격)이다.
# 핵심: 비용을 R로 환산하면 손절폭에 반비례한다. 같은 0.1%라도
#   손절폭 1% → 0.10R,  손절폭 5% → 0.02R.
# 그래서 거래별 실제 risk_pct로 나눠야 한다. 고정 R 숫자를 빼면 틀린다.
EXTRA_COST_PCT = 0.0010      # 왕복 0.10% (스프레드+충격 가정). 근거 없는 값이 아니라
                             # ADV 1천만달러 이상 종목 기준의 보수적 가정임을 명시한다.


def add_net_alpha(trade, extra_cost_pct=EXTRA_COST_PCT):
    rp = trade.get("risk_pct") or 0.0
    if "alpha_r" not in trade or rp <= 0:
        return
    trade["cost_r"] = round(extra_cost_pct / rp, 4)
    trade["net_alpha_r"] = round(trade["alpha_r"] - trade["cost_r"], 3)


# ───────────────────── 9) 순서추세 검정 + 다중검정 보정 ─────────────────────
def _spearman(xs, ys):
    n = len(xs)
    if n < 20:
        return None
    def rank(v):
        order = sorted(range(len(v)), key=lambda k: v[k])
        r = [0.0] * len(v)
        k = 0
        while k < len(order):
            j = k
            while j + 1 < len(order) and v[order[j + 1]] == v[order[k]]:
                j += 1
            avg = (k + j) / 2.0 + 1
            for m in range(k, j + 1):
                r[order[m]] = avg
            k = j + 1
        return r
    rx, ry = rank(xs), rank(ys)
    mx, my = sum(rx) / n, sum(ry) / n
    num = sum((a - mx) * (b - my) for a, b in zip(rx, ry))
    dx = sum((a - mx) ** 2 for a in rx) ** 0.5
    dy = sum((b - my) ** 2 for b in ry) ** 0.5
    return num / (dx * dy) if dx > 0 and dy > 0 else None


def trend_test(rows, feature, target="net_alpha_r", iters=1000, seed=11):
    """feature와 성과의 순서추세를 검정한다.

    판호는 Jonckheere-Terpstra를 권했는데, 여기서는 스피어만 순위상관을 쓴다.
    이유: (1) 둘 다 단조 순서 대립가설을 보는 검정이고, (2) 분위로 묶지 않고
    원값을 그대로 써서 정보 손실이 없으며, (3) 구현이 단순해 검증하기 쉽다.
    p값은 이론식으로 내지 않는다 — 우리 거래는 같은 시장 시기에 몰려 있어서
    독립 가정이 깨지기 때문이다. 대신 진입 월 단위 블록 부트스트랩으로 낸다.
    """
    data = [(t["feat"][feature], t[target], t["entry_date"][:7])
            for t in rows
            if t.get("feat", {}).get(feature) is not None and t.get(target) is not None]
    if len(data) < 100:
        return None
    rho = _spearman([d[0] for d in data], [d[1] for d in data])
    if rho is None:
        return None
    by_month = {}
    for d in data:
        by_month.setdefault(d[2], []).append(d)
    months = list(by_month)
    rnd = random.Random(seed)
    boots = []
    for _ in range(iters):
        samp = []
        for _ in range(len(months)):
            samp += by_month[months[rnd.randrange(len(months))]]
        r = _spearman([s[0] for s in samp], [s[1] for s in samp])
        if r is not None:
            boots.append(r)
    if len(boots) < iters // 2:
        return {"n": len(data), "rho": round(rho, 4)}
    boots.sort()
    lo = boots[int(0.025 * len(boots))]
    hi = boots[int(0.975 * len(boots))]
    # 양측 p: 부트스트랩 분포가 0을 넘는 비율 × 2
    side = min(sum(1 for b in boots if b <= 0), sum(1 for b in boots if b >= 0))
    p = min(1.0, 2.0 * side / len(boots))
    return {"n": len(data), "rho": round(rho, 4),
            "ci_low": round(lo, 4), "ci_high": round(hi, 4), "p": round(p, 4)}


def holm(pvals, alpha=0.05):
    """Holm step-down. 지표를 여러 개 보면 그중 하나가 우연히 유의해 보일 확률이
    커진다. BH(발견 최대화)가 아니라 Holm(오탐 1건의 비용이 큼)을 쓴다 — 여기서
    잘못 채택한 지표는 실제 매매 규칙에 들어가기 때문이다."""
    items = sorted(pvals.items(), key=lambda kv: (kv[1] is None, kv[1]))
    m = sum(1 for _, v in items if v is not None)
    out, prev, k = {}, 0.0, 0
    for name, p in items:
        if p is None:
            out[name] = {"p": None, "holm_p": None, "reject": False}
            continue
        k += 1
        adj = max(prev, min(1.0, (m - k + 1) * p))
        prev = adj
        out[name] = {"p": p, "holm_p": round(adj, 4), "reject": adj <= alpha}
    return out


# ───────────────────── 10) D전략: 돌파 후 첫 눌림 진입 ─────────────────────
# 배경 (9/11):
#   - 단순 20일 돌파는 베타 보정 후 알파가 음수였다 (-0.004 ~ -0.092R).
#   - 청산을 ema10에서 ema50/ATR5.0까지 크게 바꿔도 알파가 살아나지 않았다.
#   - VCP 지표 10개 전부 알파를 가르지 못했다 (전부 p>0.05, 홀름 통과 0개).
#   → 남은 변수는 "언제 사느냐" 하나다. 돌파 순간을 추격하는 대신
#     첫 눌림을 기다렸다가 재돌파에서 들어간다.
#
# 파라미터는 결과를 보기 전에 고정한다. 아래 숫자를 결과에 맞춰 조정하면
# 그 순간 이 검정은 무효가 된다.
PULLBACK_MIN_BARS = 2        # 눌림 최소 길이
PULLBACK_MAX_BARS = 5        # 눌림 최대 길이 (이 안에 안 끝나면 포기)
PULLBACK_MIN_DEPTH = 0.01    # 돌파 종가 대비 최소 1%는 눌려야 "눌림"으로 본다
PULLBACK_MAX_BREAK = 0.02    # 피봇을 2% 넘게 깨면 돌파 무효로 보고 포기
ENTRY_WINDOW_BARS = 5        # 눌림 끝난 뒤 재돌파를 기다리는 최대 봉 수
RETRIGGER_BUFFER = 0.003     # 재돌파 트리거 = 구조 고가 * (1 + 0.3%)
CHASE_LIMIT = 0.02           # 트리거 대비 2% 넘게 갭뜨면 미체결 처리


def collect_pullback_entries(symbol, bars, cfg: BTConfig, p: Params = None,
                             lookback=20, sma_trend=50, counters=None, stats=None):
    """20일 신고가 돌파 → 2~5일 첫 눌림(거래량 감소 동반) → 재돌파에서 진입.

    대조군(collect_entries)과 반환 형식이 같아서 뒤의 도구들이 그대로 돌아간다.
    손절이 눌림 저점 기준이라 구조적으로 돌파 추격보다 타이트하다 —
    이게 이 전략의 핵심 논리다.
    """
    p = p or Params()
    if counters is None:
        counters = new_counters()
    if stats is None:
        stats = {}
    o, h, l, c, v = (bars["open"], bars["high"], bars["low"],
                     bars["close"], bars["volume"])
    n = len(c)
    atr14 = atr_series(h, l, c, 14)
    atr_s = atr_series(h, l, c, p.atr_short)
    out = []

    def bump(k):
        stats[k] = stats.get(k, 0) + 1

    for b in range(max(lookback, sma_trend) + 2, n - 10):
        sma = sum(c[b - sma_trend + 1:b + 1]) / sma_trend
        pivot = max(h[b - lookback:b])
        if not (c[b] > pivot and c[b] > sma):
            continue
        bump("breakouts")

        vol_pre = sum(v[b - 4:b + 1]) / 5.0
        found = None
        for pe in range(b + PULLBACK_MIN_BARS, b + PULLBACK_MAX_BARS + 1):
            if pe >= n - 1:
                break
            pb_low = min(l[b + 1:pe + 1])
            if pb_low < pivot * (1 - PULLBACK_MAX_BREAK):
                bump("broke_down")
                break
            if pb_low > c[b] * (1 - PULLBACK_MIN_DEPTH):
                continue                      # 아직 충분히 안 눌렸다
            vol_pb = sum(v[b + 1:pe + 1]) / (pe - b)
            if vol_pre <= 0 or vol_pb >= vol_pre:
                bump("no_vol_dryup")
                continue                      # 눌림 중 거래량이 안 줄었다
            found = (pe, pb_low, max(h[b:pe + 1]))
            break
        if not found:
            continue
        pe, pb_low, struct_high = found
        bump("pullback_ok")

        trig = struct_high * (1 + RETRIGGER_BUFFER)
        cap = trig * (1 + CHASE_LIMIT)
        for e in range(pe + 1, min(pe + 1 + ENTRY_WINDOW_BARS, n)):
            if c[e] < pb_low:
                bump("failed_before_retrigger")
                break
            if h[e] < trig:
                continue
            px = max(o[e], trig)
            if px > cap:
                bump("gap_too_far")
                break
            entry = px * (1 + cfg.slippage_pct)
            buf = max(p.stop_atr_mult * (atr_s[e] or c[e] * 0.01), c[e] * p.stop_min_pct)
            stop = pb_low - buf
            verdict, risk = risk_gate(entry, stop, atr14[e - 1], cfg)
            counters[verdict] = counters.get(verdict, 0) + 1
            if verdict != "ok":
                break
            counters["entries"] += 1
            bump("entries")
            out.append({"symbol": symbol, "setup_idx": pe, "entry_idx": e,
                        "entry": entry, "stop": stop, "pivot": struct_high,
                        "risk": risk})
            break
        else:
            bump("no_retrigger")
    return out
