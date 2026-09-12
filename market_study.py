"""
market_study.py — Research 1 + Research 2  [사전등록 동결본 v1, 2026-09-12]

인철님·형배·판호 3자 합의로 설계를 동결하고 실행하는 스크립트.
**결과를 본 뒤 이 파일의 상수를 바꾸지 않는다.** 바꾸면 그 결과는 무효다.

════════════════════════════════════════════════════════
목표함수 (동결)
════════════════════════════════════════════════════════
  포트폴리오 MDD 50% 이내에서 CAGR 최대화.
  MDD > 50%면 CAGR이 아무리 높아도 즉시 기각.
  최종 비교 대상은 SPY 매수보유(동일 기간, 동일 total-return basis).

════════════════════════════════════════════════════════
Research 1 — 지수 노출 타이밍 / 로테이션 분해
════════════════════════════════════════════════════════
  arm  BH_SPY   SPY 매수보유                        (벤치마크)
  arm  BH_QQQ   QQQ 매수보유                        (수익 상한 참고)
  arm  A_GATE   QQQ 보유, 단 SPY종가>SMA200일 때만. 아니면 현금
  arm  B_RANK   월 1회, SPY/QQQ/IWM 중 126일 수익 1위 보유. 현금전환 없음
  arm  C_BOTH   월 1회, 위 1위 종목을 보유하되 자기 126일 수익>0 이고
                SPY종가>SMA200 일 때만. 아니면 현금
  arm  C_DEF    C_BOTH와 동일하되 유니버스에 GLD·IEF 추가 (대조군, 2005~)

  분해 판정: C_BOTH가 A_GATE보다 유의하게 낫지 않으면 크로스섹션 랭킹은 폐기.
  ※ 기본 유니버스에서 GLD/IEF를 뺀 이유: GLD 상장이 2004-11이라 포함하면
    2000~2002 구간이 표본에서 통째로 빠진다. MDD 50%를 검증하면서
    최악 구간을 제외하는 것은 검증이 아니다.

════════════════════════════════════════════════════════
Research 2 — 오버나이트 / 장중 구조 분해
════════════════════════════════════════════════════════
  (a) 진단: overnight = Open_t / Close_(t-1) - 1
            intraday  = Close_t / Open_t - 1
      각 구간의 연율수익·변동성·MDD·자기상관(lag1~5)을 분리 측정.
      → 두 구간이 서로 다른 과정인지 판정 (판호3 전제의 1차 검정)
  (b) 전략: 오버나이트 구간만 보유. 토스 LOC 매수 + 정규장예약 시가 매도.
      **LOC 체결조건을 그대로 재현한다** — 당일 종가가 직전 종가×1.03을
      초과하면 미체결로 처리하고 그날 밤은 보유하지 않는다.

════════════════════════════════════════════════════════
공통 규약 (동결)
════════════════════════════════════════════════════════
  · total-return basis: yfinance auto_adjust=True. 전 종목 동일 적용.
    Open/High/Low/Close가 모두 같은 계수로 조정되므로 Research 2에서
    'adjusted close + raw open' 혼합 오류가 발생하지 않는다.
  · 현금 수익률 0%. (실제 현금은 이자가 붙으므로 이 가정은 현금 보유 전략에
    불리하다. 즉 보수적이다.)
  · 신호는 종가 기준으로 산출하고 **다음 거래일부터 적용**한다. 미래참조 없음.
  · 비용: 편도 0.025%(왕복 0.05%)를 회전율에 비례해 차감.
    Research 2는 여기에 시가·종가 슬리피지 편도 0.02%를 추가.
  · OOS 분할: train 2000-01-01~2014-12-31 / test 2015-01-01~현재
  · 신뢰구간: 연도 블록 부트스트랩, 2000회, 95%. 두 arm을 같은 연도 시퀀스에
    적용하는 대응(paired) 방식.
  · MDD는 일별 자산곡선에서, MAR = CAGR / MDD.
  · 각 arm의 평균 노출을 계산해 **동일 평균 노출 SPY/현금 정적 혼합**을 함께 보고.
    "현금을 들고 있어서 낙폭이 작은 것"과 "타이밍이 맞은 것"을 구분하기 위함.

════════════════════════════════════════════════════════
기각 기준 (동결)
════════════════════════════════════════════════════════
  Research 1 — 목표함수와 판정식을 일치시킨다(판호 지적).
    목표함수가 "MDD 50% 제약 하 CAGR 최대화"이므로 통계 비교도 CAGR로 한다.
    MAR로 판정하면 목표함수가 조용히 'MAR 최대화'로 바뀐다.
    (예: X=CAGR25%/MDD45% 와 Y=CAGR18%/MDD25% 는 둘 다 제약을 통과하므로
     우리 목표함수에서는 X가 우선인데, MAR로 재면 Y가 이긴다.)
    TEST 구간에서 아래 중 하나라도 걸리면 해당 arm 기각:
      (1) MDD > 50%                                      ← hard constraint
      (2) CAGR <= BH_SPY의 CAGR
      (3) (arm − BH_SPY) **CAGR 차이** 의 대응 연도블록 부트스트랩 95% CI 하한 <= 0
    FULL 구간 MDD > 50% 는 기각이 아니라 **역사적 feasibility 실패**로 별도 표시.
    랭킹 추가가치: C_BOTH − A_GATE 의 **CAGR 차이** CI 하한 <= 0 이면 랭킹 폐기.
    MAR은 계속 계산하되 **보조지표로만** 보고한다.

  Research 2 — 사전등록 그대로 유지(MAR 기준, 수정 없음).
    (b) LOC 미체결일을 미보유로 반영하지 않은 결과는 무효.

════════════════════════════════════════════════════════
기간의 역할 구분 (판호 지적, 동결)
════════════════════════════════════════════════════════
  닷컴(2000~02)과 2008이 전부 train에 들어간다. 따라서
  "OOS에서 역사적 최악 MDD를 통과했다"고 해석하면 안 된다. 역할을 나눠 읽는다:
    2000~2014 (TRAIN) = 설계·역사 스트레스 구간
    2015~2026 (TEST)  = OOS 재현성 구간          ← 합격/불합격 판정은 여기서
    2000~2026 (FULL)  = 역사적 MDD 실현가능성 확인
  MDD ≤ 50% 조건은 **TEST와 FULL 양쪽에 모두** 출력하고 양쪽을 함께 본다.

════════════════════════════════════════════════════════
해석 규칙 (판호 지적, 동결)
════════════════════════════════════════════════════════
  · gate(A_GATE)가 실패해도 그것만으로 개별주 residual momentum 전략을
    **논리적으로 자동기각하지 않는다.** 전략×gate interaction이 다를 수 있다.
    gate 성공 → 해당 연구 가치 상승 / gate 실패 → 우선순위 크게 강등, 까지만.
  · 개별주 전략의 보유종목 수는 train MDD를 보고 고르지 않는다.
    실제 5슬롯 운용 제약대로 **Top 5 equal-weight 고정.**
    5종목으로 MDD > 50%면 운용조건 미달로 기각하고, N을 늘려 구제하지 않는다.
  · 지수 session decomposition(Research 2)은 개별주 cross-sectional intraday
    momentum의 검증이 **아니다.** 두 결과를 구분해서 기록한다.
"""

import json
import math
import os
import random
import sys
from datetime import date

# ─────────────── 사전등록 상수 (동결) ───────────────
TICKERS_CORE = ["SPY", "QQQ", "IWM"]
TICKERS_DEF = ["GLD", "IEF"]
SMA_LEN = 200
LOOKBACK = 126
COST_ONE_WAY = 0.00025          # 왕복 0.05%
SLIP_ONE_WAY = 0.0002           # Research 2 전용 추가
LOC_LIMIT_MULT = 1.03
TRAIN_START = "2000-01-01"
TRAIN_END = "2014-12-31"
TEST_START = "2015-01-01"
BOOT_ITERS = 2000
BOOT_SEED = 20260912
MDD_LIMIT = 0.50
TRADING_DAYS = 252


# ─────────────── 기본 통계 ───────────────

def equity_curve(rets):
    """일별 수익률 리스트 → 자산곡선(시작 1.0)."""
    eq, v = [], 1.0
    for r in rets:
        v *= (1.0 + r)
        eq.append(v)
    return eq


def max_drawdown(eq):
    peak, mdd = -1e18, 0.0
    for v in eq:
        if v > peak:
            peak = v
        if peak > 0:
            dd = 1.0 - v / peak
            if dd > mdd:
                mdd = dd
    return mdd


def cagr_from(eq, n_days):
    if not eq or n_days <= 0 or eq[-1] <= 0:
        return float("nan")
    years = n_days / TRADING_DAYS
    if years <= 0:
        return float("nan")
    return eq[-1] ** (1.0 / years) - 1.0


def stdev(xs):
    n = len(xs)
    if n < 2:
        return 0.0
    m = sum(xs) / n
    return math.sqrt(sum((x - m) ** 2 for x in xs) / (n - 1))


def summarize(rets):
    """일별 수익률 → CAGR / MDD / MAR / 연율변동성."""
    if not rets:
        return {"cagr": None, "mdd": None, "mar": None, "vol": None, "n": 0}
    eq = equity_curve(rets)
    c = cagr_from(eq, len(rets))
    m = max_drawdown(eq)
    return {
        "cagr": c,
        "mdd": m,
        "mar": (c / m) if (m and m > 1e-9 and c == c) else None,
        "vol": stdev(rets) * math.sqrt(TRADING_DAYS),
        "n": len(rets),
        "final": eq[-1],
    }


def autocorr(xs, lag):
    n = len(xs) - lag
    if n < 30:
        return None
    a = xs[:-lag]
    b = xs[lag:]
    ma, mb = sum(a) / len(a), sum(b) / len(b)
    num = sum((a[i] - ma) * (b[i] - mb) for i in range(n))
    da = math.sqrt(sum((x - ma) ** 2 for x in a))
    db = math.sqrt(sum((x - mb) ** 2 for x in b))
    if da <= 0 or db <= 0:
        return None
    return num / (da * db)


# ─────────────── 연도 블록 부트스트랩 (대응) ───────────────

def yearly_blocks(dates, *series):
    """연도별로 (연도 -> [인덱스]) 를 만들고 각 시리즈를 그 연도 조각으로 자른다."""
    order, buckets = [], {}
    for i, d in enumerate(dates):
        y = d[:4]
        if y not in buckets:
            buckets[y] = []
            order.append(y)
        buckets[y].append(i)
    out = []
    for y in order:
        idx = buckets[y]
        out.append(tuple([s[i] for i in idx] for s in series))
    return out


def boot_diff(dates, rets_a, rets_b, metric="cagr", iters=BOOT_ITERS, seed=BOOT_SEED):
    """(a의 metric - b의 metric) 의 연도 블록 부트스트랩 95% CI.
    같은 연도 시퀀스를 양쪽에 적용하는 대응(paired) 방식.

    metric="cagr" 가 Research 1의 판정 기준이다. 목표함수가
    "MDD 50% 제약 하 CAGR 최대화"이므로 판정식도 CAGR이어야 한다.
    MAR로 판정하면 목표함수가 조용히 'MAR 최대화'로 바뀐다(판호 지적).
    MAR은 계속 계산하되 보조지표로만 보고한다.
    """
    blocks = yearly_blocks(dates, rets_a, rets_b)
    if len(blocks) < 3:
        return None
    rng = random.Random(seed)
    diffs = []
    nb = len(blocks)
    for _ in range(iters):
        pick = [blocks[rng.randrange(nb)] for _ in range(nb)]
        ra, rb = [], []
        for pa, pb in pick:
            ra.extend(pa)
            rb.extend(pb)
        sa, sb = summarize(ra), summarize(rb)
        va, vb = sa.get(metric), sb.get(metric)
        if va is None or vb is None or va != va or vb != vb:
            continue
        diffs.append(va - vb)
    if len(diffs) < iters * 0.5:
        return None
    diffs.sort()
    lo = diffs[int(0.025 * len(diffs))]
    hi = diffs[int(0.975 * len(diffs)) - 1]
    return {"metric": metric, "lo": lo, "hi": hi,
            "median": diffs[len(diffs) // 2], "n": len(diffs)}


def boot_mar_diff(dates, rets_a, rets_b, iters=BOOT_ITERS, seed=BOOT_SEED):
    """Research 2 전용(사전등록이 MAR 기준). Research 1은 boot_diff(metric='cagr')."""
    return boot_diff(dates, rets_a, rets_b, "mar", iters, seed)


# ─────────────── 포지션 → 수익률 (비용 포함) ───────────────

def apply_positions(dates, close_by_sym, positions, cost_one_way=COST_ONE_WAY):
    """
    positions[i] = 날짜 i 하루 동안 보유할 종목명 또는 None(현금).
    수익률은 close[i-1] -> close[i]. positions[i]는 i-1 종가에 결정된 값이어야 한다.
    종목이 바뀌면 그날 편도비용 2회(매도+매수), 현금↔종목이면 1회를 차감한다.
    """
    rets = []
    prev_pos = None
    for i in range(1, len(dates)):
        pos = positions[i]
        r = 0.0
        if pos is not None:
            c0 = close_by_sym[pos][i - 1]
            c1 = close_by_sym[pos][i]
            if c0 and c0 > 0 and c1 and c1 > 0:
                r = c1 / c0 - 1.0
            else:
                pos = None
                r = 0.0
        legs = 0
        if prev_pos != pos:
            if prev_pos is not None:
                legs += 1
            if pos is not None:
                legs += 1
        r -= legs * cost_one_way
        rets.append(r)
        prev_pos = pos
    return rets


def exposure_ratio(positions):
    live = sum(1 for p in positions[1:] if p is not None)
    return live / max(len(positions) - 1, 1)


# ─────────────── 신호 ───────────────

def sma_series(vals, n):
    out, s = [None] * len(vals), 0.0
    for i, v in enumerate(vals):
        s += v
        if i >= n:
            s -= vals[i - n]
        if i >= n - 1:
            out[i] = s / n
    return out


def month_end_flags(dates):
    flags = [False] * len(dates)
    for i in range(len(dates) - 1):
        if dates[i][:7] != dates[i + 1][:7]:
            flags[i] = True
    if dates:
        flags[-1] = True
    return flags


def build_positions(dates, close_by_sym, universe, use_gate, use_rank,
                    fixed_symbol=None):
    """
    신호는 i일 종가로 산출하고 i+1일부터 적용한다(미래참조 없음).
    use_rank=False  → fixed_symbol 을 계속 보유 대상으로 삼는다.
    use_rank=True   → 월말에 126일 수익 1위로 교체.
    use_gate=True   → SPY 종가 > SMA200 이고 (랭킹이면) 자기 126일 수익 > 0 일 때만 보유.
    """
    spy = close_by_sym["SPY"]
    spy_sma = sma_series(spy, SMA_LEN)
    me = month_end_flags(dates)

    positions = [None] * len(dates)
    current = None
    for i in range(len(dates) - 1):
        # --- i일 종가 시점의 정보만 사용 ---
        if use_rank:
            if me[i] or current is None:
                best, best_r = None, None
                for s in universe:
                    cs = close_by_sym[s]
                    if i < LOOKBACK or not cs[i] or not cs[i - LOOKBACK]:
                        continue
                    r = cs[i] / cs[i - LOOKBACK] - 1.0
                    if best_r is None or r > best_r:
                        best, best_r = s, r
                if best is not None:
                    current = best
        else:
            current = fixed_symbol

        want = current
        if want is not None:
            cs = close_by_sym[want]
            if not cs[i] or i < LOOKBACK or not cs[i - LOOKBACK]:
                want = None
        if want is not None and use_gate:
            if spy_sma[i] is None or spy[i] is None or spy[i] <= spy_sma[i]:
                want = None
            elif use_rank:
                cs = close_by_sym[want]
                if cs[i] / cs[i - LOOKBACK] - 1.0 <= 0:
                    want = None
        positions[i + 1] = want
    return positions


def static_blend(dates, close_by_sym, exposure):
    """동일 평균 노출 SPY/현금 정적 혼합. 매일 리밸런싱, 비용 없음(보수적으로 우리에게 불리)."""
    spy = close_by_sym["SPY"]
    rets = []
    for i in range(1, len(dates)):
        c0, c1 = spy[i - 1], spy[i]
        r = (c1 / c0 - 1.0) if (c0 and c1 and c0 > 0) else 0.0
        rets.append(r * exposure)
    return rets


# ─────────────── Research 2 ───────────────

def overnight_intraday(dates, o, h, l, c):
    """overnight = Open_t/Close_(t-1)-1, intraday = Close_t/Open_t-1. 인덱스 1부터."""
    on, itd, d = [], [], []
    for i in range(1, len(dates)):
        if not (o[i] and c[i - 1] and c[i]) or o[i] <= 0 or c[i - 1] <= 0:
            continue
        on.append(o[i] / c[i - 1] - 1.0)
        itd.append(c[i] / o[i] - 1.0)
        d.append(dates[i])
    return d, on, itd


def overnight_strategy(dates, o, c, cost_mult=1.0):
    """
    토스 LOC 매수 + 정규장예약 시가 매도.
    i-1일 종가에 매수 시도: 종가가 직전 종가×LOC_LIMIT_MULT 이하일 때만 체결.
    i일 시가에 매도.
    비용: 매수·매도 각각 편도비용 + 슬리피지.

    cost_mult 는 **보고용 스트레스 배수**다. 합격/불합격 판정은 사전등록대로
    cost_mult=1.0 결과로만 내린다. 1.5배·3배는 정보로만 남긴다
    (실제 토스 수수료+환전스프레드가 가정보다 클 수 있어 민감도를 남겨둔다).
    """
    unit = 2 * (COST_ONE_WAY + SLIP_ONE_WAY) * cost_mult
    d, rets, filled, skipped = [], [], 0, 0
    for i in range(2, len(dates)):
        prev_c, buy_c, open_next = c[i - 2], c[i - 1], o[i]
        if not (prev_c and buy_c and open_next) or prev_c <= 0 or buy_c <= 0:
            continue
        d.append(dates[i])
        if buy_c > prev_c * LOC_LIMIT_MULT:      # LOC 미체결 → 그날 밤 미보유
            skipped += 1
            rets.append(0.0)
            continue
        filled += 1
        rets.append((open_next / buy_c - 1.0) - unit)
    return d, rets, filled, skipped


# ─────────────── 구간 자르기 / 보고 ───────────────

def slice_period(dates, rets, start, end):
    """dates/rets 길이가 같다고 가정(둘 다 i>=1 기준으로 맞춰 넘길 것)."""
    ds, rs = [], []
    for d, r in zip(dates, rets):
        if start <= d <= end:
            ds.append(d)
            rs.append(r)
    return ds, rs


def verdict(arm, bench, ci):
    """Research 1 판정. 목표함수(MDD 50% 제약 하 CAGR 최대화)와 판정식을 일치시킨다.

    (1) hard constraint : TEST MDD > 50% → 기각
    (2) 성과            : CAGR <= BH_SPY → 기각
    (3) 통계적 비교     : (arm − BH_SPY) **CAGR 차이** CI 하한 <= 0 → 기각
    FULL 기간 MDD 초과는 기각이 아니라 '역사적 feasibility 실패'로 별도 표시한다.
    """
    reasons = []
    if arm["mdd"] is not None and arm["mdd"] > MDD_LIMIT:
        reasons.append(f"MDD {arm['mdd']*100:.1f}% > 50%")
    if arm["cagr"] is not None and bench["cagr"] is not None and arm["cagr"] <= bench["cagr"]:
        reasons.append("CAGR <= SPY")
    if ci is None:
        reasons.append("CI 계산 불가")
    elif ci["lo"] <= 0:
        reasons.append(f"CAGR차 CI 하한 {ci['lo']*100:+.2f}%p <= 0")
    return ("PASS" if not reasons else "REJECT"), reasons


def fmt(s):
    if s is None:
        return "  -  "
    return f"{s*100:6.2f}%" if abs(s) < 10 else f"{s:8.2f}"
