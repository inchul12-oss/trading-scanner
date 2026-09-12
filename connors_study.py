"""
connors_study.py — Connors ETF 평균회귀 2종 검증 [사전등록 동결본 v1, 2026-09-12]

판호 선택 (a): 원 규칙 그대로 돌리고, 합격기준을 낮추지 않는다.
  1) Double 7's        2) RSI 25
  3-Day High/Low 는 원 규칙을 토스 주문으로 사전 구현할 수 없어 후보에서 제외.

합격기준(변경 금지):  TEST 구간에서
  (1) MDD <= 50%                      ← hard constraint
  (2) CAGR > SPY 매수보유
  (3) (전략 - SPY) CAGR 차이의 대응 연도블록 부트스트랩 95% CI 하한 > 0
  낙폭이 아무리 좋아도 CAGR 미달은 기각. 결과를 본 뒤 레버리지·포지션 확대를 붙이지 않는다.
  FULL 구간 MDD 초과는 기각이 아니라 '역사적 feasibility 실패'로 별도 표시.

────────────────────────────────────────────────────────
원 규칙
────────────────────────────────────────────────────────
■ Double 7's (Connors / Alvarez)
   진입: 종가 > SMA200  AND  종가가 최근 7거래일 최저 종가 → 그날 종가에 매수
   청산: 종가가 최근 7거래일 최고 종가가 된 날 종가에 전량 매도
   고정 손절 없음.

■ RSI 25 (Connors, 2003 / 『High Probability ETF Trading』)
   진입: 종가 > SMA200  AND  RSI(4) < 25 → 그날 종가에 1단위 매수
         보유 중 RSI(4) < 20 이면 1단위 추가매수
   청산: RSI(4)가 55를 상향 돌파한 날 종가에 전량 매도
   고정 손절 없음.

────────────────────────────────────────────────────────
구현 선택 — 규칙에 없어서 내가 정해야 했던 것 (사전 고정, 결과 보고 안 바꿈)
────────────────────────────────────────────────────────
 (i) RSI 25 의 "1단위"
     레버리지가 없으므로 **1단위 = 계좌의 50%**, 추가매수까지 최대 100%로 고정한다.
     참고용으로 "첫 진입 100%(추가매수 무시)" 변형도 계산해 **보고만** 하고,
     합격 판정에는 쓰지 않는다.
 (ii) RSI 정의: Wilder 평활(표준). 최초 n개 변화량의 단순평균으로 시드.
 (iii) 청산 조건 "55 상향 돌파": RSI(4) > 55 인 날 종가에 청산.
 (iv) 비용: LOC 주문을 쓰므로 Research 2 와 동일하게
      편도 0.025% + LOC 슬리피지 편도 0.02% (왕복 0.09%).
      참고용으로 왕복 0.05% 변형도 **보고만** 한다.

────────────────────────────────────────────────────────
같은 봉 종가 체결이 미래참조가 아닌 이유 (중요)
────────────────────────────────────────────────────────
 두 전략 모두 트리거가 **그날 종가의 단조함수**라, "그 조건을 만족시키는 종가 P"를
 장중에 미리 계산해 토스 LOC(마감가가 P 이하면 주문)로 걸어둘 수 있다.
   · Double 7's 매수 : P = 지난 6일 최저 종가   (정확히 일치)
   · RSI 25 매수     : RSI(4)=25 가 되는 종가를 역산 (RSI는 당일 종가에 단조증가)
 따라서 같은 봉 종가 체결은 실제로 실행 가능하며, 백테스트에서도 미래참조가 아니다.
 이 단조성은 test_connors.py 에서 수치로 검증한다.
"""

import market_study as M

# ── 사전등록 상수 (동결) ──
SMA_LEN = 200
D7_LEN = 7
RSI_LEN = 4
RSI_ENTRY = 25.0
RSI_ADD = 20.0
RSI_EXIT = 55.0
UNIT = 0.5                      # RSI25 1단위 = 계좌 50%
COST_ONE_WAY = 0.00025
SLIP_ONE_WAY = 0.0002


# ─────────────── 지표 ───────────────

def wilder_rsi(closes, n=RSI_LEN):
    """Wilder 평활 RSI. out[i]는 closes[0..i]만 사용한다(미래참조 없음)."""
    out = [None] * len(closes)
    if len(closes) <= n:
        return out
    gains = losses = 0.0
    for i in range(1, n + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    ag, al = gains / n, losses / n
    out[n] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    for i in range(n + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        ag = (ag * (n - 1) + max(d, 0.0)) / n
        al = (al * (n - 1) + max(-d, 0.0)) / n
        out[i] = 100.0 if al == 0 else 100.0 - 100.0 / (1 + ag / al)
    return out


def rolling_min(xs, n):
    out = [None] * len(xs)
    for i in range(len(xs)):
        if i >= n - 1:
            out[i] = min(xs[i - n + 1:i + 1])
    return out


def rolling_max(xs, n):
    out = [None] * len(xs)
    for i in range(len(xs)):
        if i >= n - 1:
            out[i] = max(xs[i - n + 1:i + 1])
    return out


# ─────────────── 전략 → 일별 비중 ───────────────

def weights_double7(closes):
    """Double 7's. w[i] = i일 종가 체결 후 보유 비중(0 또는 1)."""
    sma = M.sma_series(closes, SMA_LEN)
    lo = rolling_min(closes, D7_LEN)
    hi = rolling_max(closes, D7_LEN)
    w, pos = [0.0] * len(closes), 0.0
    for i in range(len(closes)):
        if sma[i] is None or lo[i] is None:
            w[i] = 0.0
            continue
        if pos > 0 and closes[i] >= hi[i]:          # 7일 최고 종가 → 전량 청산
            pos = 0.0
        elif pos == 0 and closes[i] > sma[i] and closes[i] <= lo[i]:
            pos = 1.0                                # 7일 최저 종가 → 진입
        w[i] = pos
    return w


def weights_rsi25(closes, unit=UNIT):
    """RSI 25. w[i] = i일 종가 체결 후 보유 비중(0, unit, 2*unit)."""
    sma = M.sma_series(closes, SMA_LEN)
    rsi = wilder_rsi(closes, RSI_LEN)
    w, pos = [0.0] * len(closes), 0.0
    cap = 2 * unit
    for i in range(len(closes)):
        if sma[i] is None or rsi[i] is None:
            w[i] = 0.0
            continue
        if pos > 0 and rsi[i] > RSI_EXIT:            # 55 상향 돌파 → 전량 청산
            pos = 0.0
        elif closes[i] > sma[i]:
            if pos == 0 and rsi[i] < RSI_ENTRY:
                pos = unit
            elif 0 < pos < cap and rsi[i] < RSI_ADD:
                pos = min(pos + unit, cap)
        w[i] = pos
    return w


# ─────────────── 비중 → 수익률 ───────────────

def returns_from_weights(closes, w, cost_one_way=COST_ONE_WAY, slip_one_way=SLIP_ONE_WAY):
    """
    w[i]는 i일 종가에 체결을 마친 뒤의 비중이다.
    i+1일 수익 = w[i] * (close[i+1]/close[i] - 1).
    비중이 바뀐 날엔 |변화량| 에 비례해 편도비용+슬리피지를 차감한다.
    """
    unit_cost = cost_one_way + slip_one_way
    rets, prev = [], 0.0
    for i in range(1, len(closes)):
        c0, c1 = closes[i - 1], closes[i]
        mkt = (c1 / c0 - 1.0) if (c0 and c1 and c0 > 0) else 0.0
        r = w[i - 1] * mkt - abs(w[i] - w[i - 1]) * unit_cost
        rets.append(r)
        prev = w[i]
    return rets


def exposure(w):
    return sum(w[:-1]) / max(len(w) - 1, 1)


def trade_count(w):
    return sum(1 for i in range(1, len(w)) if w[i] > 0 and w[i - 1] == 0)


# ═══════════════ 실행부 (GitHub Actions) ═══════════════

def _fetch(tickers):
    import yfinance as yf
    out = {}
    for t in tickers:
        for a in range(3):
            try:
                df = yf.download(t, period="max", interval="1d",
                                 auto_adjust=True, progress=False, threads=False)
                if df is None or len(df) == 0:
                    raise RuntimeError("빈 데이터")
                if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
                    df.columns = df.columns.get_level_values(0)
                df = df.dropna()
                out[t] = {"date": [str(x)[:10] for x in df.index],
                          "close": [float(x) for x in df["Close"]]}
                print(f"  {t}: {len(df)}봉  {out[t]['date'][0]} ~ {out[t]['date'][-1]}")
                break
            except Exception as e:
                print(f"  {t} 실패({a+1}/3): {e}")
    return out


def _eval(tag, dates, rets, bench_dates, bench_rets, start, end, w, block):
    d, rr = M.slice_period(dates, rets, start, end)
    _, br = M.slice_period(bench_dates, bench_rets, start, end)
    if len(rr) < 200 or len(rr) != len(br):
        print(f"  {tag:26s} (표본 부족/기간 불일치 {len(rr)} vs {len(br)})")
        return None
    s, b = M.summarize(rr), M.summarize(br)
    ci = M.boot_diff(d, rr, br, "cagr")
    v, reasons = M.verdict(s, b, ci)
    cis = f"CI[{ci['lo']*100:+.2f},{ci['hi']*100:+.2f}]%p" if ci else "CI[계산불가]"
    print(f"  {tag:26s} CAGR {M.fmt(s['cagr'])} MDD {M.fmt(s['mdd'])} "
          f"MAR {(s['mar'] if s['mar'] is not None else float('nan')):5.2f} "
          f"노출 {exposure(w)*100:5.1f}% 거래 {trade_count(w):4d}건  "
          f"{cis}  {v}"
          + (f"  ({'; '.join(reasons)})" if reasons else ""))
    block[tag] = {"stats": s, "bench": b, "ci_cagr_vs_spy": ci,
                  "verdict": v, "reasons": reasons,
                  "exposure": exposure(w), "trades": trade_count(w)}
    return s


def main():
    import json
    from datetime import datetime, timezone
    t0 = datetime.now(timezone.utc)
    print("1) 데이터")
    raw = _fetch(["SPY", "QQQ"])
    if "SPY" not in raw:
        json.dump({"error": "SPY 확보 실패"}, open("connors_result.json", "w")); return

    out = {"run_at_utc": t0.isoformat(), "preregistered": {
        "sma": SMA_LEN, "d7": D7_LEN, "rsi_len": RSI_LEN,
        "rsi_entry": RSI_ENTRY, "rsi_add": RSI_ADD, "rsi_exit": RSI_EXIT,
        "unit": UNIT, "cost_one_way": COST_ONE_WAY, "slip_one_way": SLIP_ONE_WAY,
        "pass": "TEST에서 MDD<=50% AND CAGR>SPY AND CAGR차 CI하한>0"}}

    for t in [x for x in ("SPY", "QQQ") if x in raw]:
        dates, cl = raw[t]["date"], raw[t]["close"]
        warm = SMA_LEN + 5
        if len(dates) < warm + 500:
            continue
        first = dates[warm]
        bh_w = [0.0] * warm + [1.0] * (len(dates) - warm)     # 동일 출발선
        bh = returns_from_weights(cl, bh_w, 0.0, 0.0)
        rdates = dates[1:]

        variants = [
            ("Double7", weights_double7(cl), COST_ONE_WAY, SLIP_ONE_WAY),
            ("RSI25", weights_rsi25(cl, UNIT), COST_ONE_WAY, SLIP_ONE_WAY),
            # 아래 둘은 보고용 참고치. 합격 판정에 쓰지 않는다.
            ("[참고]RSI25_full100", weights_rsi25(cl, 1.0), COST_ONE_WAY, SLIP_ONE_WAY),
            ("[참고]Double7_저비용", weights_double7(cl), COST_ONE_WAY, 0.0),
            ("[참고]RSI25_저비용", weights_rsi25(cl, UNIT), COST_ONE_WAY, 0.0),
        ]
        for label, per, st, en in (("TRAIN", first, M.TRAIN_END),
                                   ("TEST", M.TEST_START, "2100-01-01"),
                                   ("FULL", first, "2100-01-01")):
            key = f"{t}_{label}"
            print(f"\n{'='*96}\n[{key}]  {per} ~ {en}   "
                  f"(SPY매수보유 기준, 워밍업 이후 동일 출발선)\n{'='*96}")
            block = {}
            bs = M.summarize(M.slice_period(rdates, bh, per, en)[1])
            print(f"  {'BH_'+t:26s} CAGR {M.fmt(bs['cagr'])} MDD {M.fmt(bs['mdd'])} "
                  f"MAR {(bs['mar'] if bs['mar'] is not None else float('nan')):5.2f} "
                  f"노출 100.0%")
            block["BH"] = {"stats": bs}
            for name, w, c1, s1 in variants:
                _eval(name, rdates, returns_from_weights(cl, w, c1, s1),
                      rdates, bh, per, en, w, block)
            out[key] = block

        # FULL MDD feasibility 별도 표시
        tb, fb = out.get(f"{t}_TEST"), out.get(f"{t}_FULL")
        if tb and fb:
            for k, v in tb.items():
                fm = fb.get(k, {}).get("stats", {}).get("mdd")
                v["full_mdd"] = fm
                v["full_mdd_feasible"] = (fm is not None and fm <= M.MDD_LIMIT)

    out["elapsed_sec"] = (datetime.now(timezone.utc) - t0).total_seconds()
    with open("connors_result.json", "w") as f:
        json.dump(out, f, indent=1, ensure_ascii=False, default=str)
    print(f"\n완료 ({out['elapsed_sec']:.0f}초). connors_result.json 저장")


if __name__ == "__main__":
    main()
