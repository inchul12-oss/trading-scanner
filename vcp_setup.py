"""
VCP(변동성 수축 패턴) 셋업 탐지 — 1층(SETUP) 순수 로직.

설계 원칙 (2026-09-09 확정):
1) **미래를 절대 보지 않는다.** 모든 함수는 (bars, i)를 받고 bars[0..i]만 사용한다.
   i는 "i번째 일봉이 마감된 시점"을 뜻한다. 백테스트와 라이브가 완전히 같은 함수를
   쓰기 때문에 backtest-live divergence가 구조적으로 생기지 않는다.

2) **베이스 구간을 탐색(search)하지 않고 결정(derive)한다.**
   판호가 지적한 문제 — 10~50일 중 "가장 좋은 window"를 매일 다시 고르면 새 봉 하나에
   피봇이 소급 변경될 수 있음 — 을 window 탐색 자체를 없애서 근본적으로 제거했다.
   베이스는 "최근 MAX_BASE일 중 최고가가 나온 봉"에서 시작한다. 이 정의는 결정론적이고,
   새 봉이 추가돼도 신고가가 나오지 않는 한 베이스 시작점과 피봇이 절대 바뀌지 않는다.
   신고가가 나오면 베이스가 진짜로 리셋된 것이므로 바뀌는 게 맞다.

3) **필터를 늘리지 않고 특징(feature)을 기록한다.**
   파라미터 개수를 늘리면 curve fitting 위험이 커지므로, v1은 최소한의 하드 조건만 두고
   나머지는 전부 수치로 남겨 백테스트 후 사후 분석한다.

4) **긴 히스토리를 요구하는 조건은 데이터가 없으면 스킵한다.**
   과거에 200일선 조건 때문에 신규상장주가 통째로 평가 대상에서 빠지는 버그가 있었다.
   같은 실수를 반복하지 않기 위해 SMA150 / 52주 고가 조건은 데이터 부족 시 스킵하고,
   스킵됐다는 사실을 결과에 남긴다.
"""

from dataclasses import dataclass, asdict


# ─────────────────────────────── 파라미터 ───────────────────────────────
@dataclass
class Params:
    # 베이스 길이 (거래일)
    min_base: int = 10
    max_base: int = 50

    # 추세 조건
    sma_fast: int = 50
    sma_slow: int = 150
    prior_advance_lookback: int = 60      # 베이스 직전 저점을 찾는 구간
    prior_advance_min: float = 0.20       # 베이스 진입 전 최소 상승률 (+20%)
    high_52w_lookback: int = 252
    high_52w_min_ratio: float = 0.80      # 52주 고가의 80% 이상

    # 수축 조건
    swing_k: int = 2                      # 스윙 저점 판정 폭 (좌우 k봉)
    min_contraction_depth: float = 0.02   # 이보다 얕은 눌림은 노이즈로 무시
    min_contractions: int = 2             # 최소 수축 횟수
    contraction_ratio_max: float = 0.80   # 다음 수축 깊이 / 이전 수축 깊이

    # 타이트함 / 변동성 / 거래량
    range10_max: float = 0.10             # 최근 10일 고저폭
    range5_max: float = 0.06              # 최근 5일 고저폭
    atr_short: int = 5
    atr_long: int = 20
    # 수축 측정: NATR5(=ATR5/종가)의 중앙값 비교. 최근 3일 중앙값 / 베이스 시작 직전 5일 중앙값.
    atr_contraction_max: float = 0.75     # v1 기본값 (grid: 0.65 / 0.75 / 0.85)
    natr_median_span_base: int = 5
    natr_median_span_now: int = 3
    vol_short: int = 5
    vol_long: int = 20
    vol_ratio_max: float = 0.70           # 최근5일 평균거래량 / 그 이전 20일 평균거래량

    # 손절 / 진입 상한
    stop_atr_mult: float = 0.25           # 손절 버퍼 = max(0.25*ATR5, 종가*0.3%)
    stop_min_pct: float = 0.003
    chase_limit: float = 0.02             # 피봇 대비 최대 추격 +2%
    max_structural_risk: float = 0.05     # 진입가 대비 구조적 손절까지 최대 5%


# ─────────────────────────────── 유틸 ───────────────────────────────
def _sma(vals, n, i):
    if i + 1 < n:
        return None
    seg = vals[i - n + 1: i + 1]
    return sum(seg) / n


def _true_range(high, low, close, j):
    if j == 0:
        return high[j] - low[j]
    pc = close[j - 1]
    return max(high[j] - low[j], abs(high[j] - pc), abs(low[j] - pc))


def _atr(high, low, close, n, i):
    """단순평균 ATR. i봉 마감 기준, 최근 n개 TR의 평균."""
    if i + 1 < n + 1:
        return None
    trs = [_true_range(high, low, close, j) for j in range(i - n + 1, i + 1)]
    return sum(trs) / n


def _find_swing_lows(low, start, end, k):
    """[start, end] 구간에서 스윙 저점 인덱스를 찾는다.
    j가 스윙 저점 = low[j]가 [j-k, j+k] 안에서 최소. j+k <= end 이므로 미래를 안 본다."""
    out = []
    for j in range(start + k, end - k + 1):
        seg = low[j - k: j + k + 1]
        if low[j] == min(seg):
            # 동일 최저값이 연속될 때 첫 번째만 채택
            if not out or j - out[-1] > k:
                out.append(j)
    return out


# ─────────────────────────────── 본체 ───────────────────────────────
def evaluate_vcp(bars, i, p: Params = None):
    """bars: dict of lists — 'high','low','close','volume' (모두 같은 길이, 오름차순 날짜)
    i: 평가 기준 봉 인덱스 (이 봉이 마감된 시점). bars[0..i]만 사용한다.
    반환: 판정 결과 + 모든 특징값이 담긴 dict."""
    p = p or Params()
    high, low, close, vol = bars["high"], bars["low"], bars["close"], bars["volume"]

    r = {"i": i, "ready": False, "reject": None, "skipped": []}

    if i < p.max_base + p.atr_long + 5:
        r["reject"] = "히스토리 부족"
        r["reject_code"] = "history_short"
        return r

    c = close[i]
    r["close"] = c

    # ── 1. 추세 필터 ──────────────────────────────────────────────
    sma50 = _sma(close, p.sma_fast, i)
    sma150 = _sma(close, p.sma_slow, i)
    r["sma50"], r["sma150"] = sma50, sma150

    if sma50 is None:
        r["reject"] = "SMA50 계산 불가"
        r["reject_code"] = "sma50_calc"
        return r
    if c <= sma50:
        r["reject"] = "추세: 종가가 50일선 아래"
        r["reject_code"] = "trend_below_sma50"
        return r

    if sma150 is None:
        r["skipped"].append("sma150")          # 신규상장주 — 스킵(자동 통과)
    elif sma50 <= sma150:
        r["reject"] = "추세: 50일선이 150일선 아래"
        r["reject_code"] = "trend_sma50_below_sma150"
        return r

    if i + 1 >= p.high_52w_lookback:
        h52 = max(high[i - p.high_52w_lookback + 1: i + 1])
        r["pct_of_52w_high"] = c / h52
        if c < h52 * p.high_52w_min_ratio:
            r["reject"] = "추세: 52주 고가 대비 너무 낮음"
            r["reject_code"] = "trend_below_52w"
            return r
    else:
        r["skipped"].append("high_52w")

    # ── 2. 베이스 구간 결정 (탐색 아님) ────────────────────────────
    # 9/11 정정: 판호 지적대로 이 앵커는 "신고가가 나와야만 바뀐다"가 아니다.
    # 50일 롤링 창이 밀리면서 기존 최고가가 창 밖으로 빠지면 신고가 없이도 앵커가 이동한다.
    # (합성데이터로 재현 확인: 앵커 변경 11회 중 2회가 신고가 없이 발생)
    # 이건 "50일 넘은 베이스는 폐기한다"는 의미이므로 동작 자체는 옳지만,
    # 이미 주문을 걸어둔 셋업의 피봇이 바뀔 수 있다는 뜻이므로 주문 기록(setup_id)은 반드시 필요하다.
    lo_idx = i - p.max_base + 1
    seg_high = high[lo_idx: i + 1]
    base_high_idx = lo_idx + seg_high.index(max(seg_high))
    base_len = i - base_high_idx
    base_anchor_high = high[base_high_idx]

    r["base_start_idx"], r["base_len"] = base_high_idx, base_len
    r["base_anchor_high"] = base_anchor_high

    if base_len < p.min_base:
        r["reject"] = f"베이스 길이 부족({base_len}일)"
        r["reject_code"] = "base_too_short"
        return r

    # 베이스 진입 전 상승폭
    adv_lo = max(0, base_high_idx - p.prior_advance_lookback)
    if adv_lo < base_high_idx:
        prior_low = min(low[adv_lo: base_high_idx])
        prior_advance = base_anchor_high / prior_low - 1
        r["prior_advance"] = prior_advance
        if prior_advance < p.prior_advance_min:
            r["reject"] = f"베이스 직전 상승 부족({prior_advance:.1%})"
            r["reject_code"] = "prior_advance_low"
            return r
    else:
        r["skipped"].append("prior_advance")

    # ── 3. 수축 구조 ──────────────────────────────────────────────
    swings = _find_swing_lows(low, base_high_idx, i, p.swing_k)
    depths, kept = [], []
    running_high = base_anchor_high
    for j in swings:
        running_high = max(running_high, max(high[base_high_idx: j + 1]))
        d = (running_high - low[j]) / running_high
        if d >= p.min_contraction_depth:
            depths.append(d)
            kept.append(j)
    r["contraction_depths"] = [round(d, 4) for d in depths]
    r["contraction_idx"] = kept
    r["n_contractions"] = len(depths)

    if len(depths) < p.min_contractions:
        r["reject"] = f"수축 횟수 부족({len(depths)}회)"
        r["reject_code"] = "contractions_few"
        return r

    ratios = [depths[k + 1] / depths[k] for k in range(len(depths) - 1)]
    r["contraction_ratios"] = [round(x, 3) for x in ratios]
    # 판호 제안(9/11): 매 구간 비율을 엄격히 요구하는 것보다 "처음 대비 마지막이 얼마나
    # 압축됐나"가 더 robust할 수 있다(예: 18→15→7은 중간이 안 줄었지만 최종 압축은 명확).
    # 지금은 규칙을 바꾸지 않고 기록만 한다.
    r["depth_first"], r["depth_final"] = round(depths[0], 4), round(depths[-1], 4)
    r["depth_final_over_first"] = round(depths[-1] / depths[0], 3)
    r["contraction_ratio_max_observed"] = round(max(ratios), 3) if ratios else None
    if any(x > p.contraction_ratio_max for x in ratios):
        r["reject"] = f"수축이 점점 좁아지지 않음(비율 {max(ratios):.2f})"
        r["reject_code"] = "contraction_not_tightening"
        return r

    final_contraction_low = low[kept[-1]]
    r["final_contraction_low"] = final_contraction_low

    # 9/11 판호 제안 채택: 베이스 앵커(구간 시작점)와 진입 피봇(매수 트리거)을 분리한다.
    # 앵커 고가를 그대로 트리거로 쓰면 베이스 초반의 일회성 꼬리가 트리거가 되어
    # 진입이 지나치게 늦어지고 손절까지 거리가 멀어져 R:R이 나빠진다.
    # 진입 피봇 = 마지막 수축 구간의 고가 = 직전 스윙저점~마지막 스윙저점 사이의 최고가.
    seg_lo = kept[-2] if len(kept) >= 2 else base_high_idx
    pivot = max(high[seg_lo: kept[-1] + 1])
    r["pivot"] = pivot
    r["pivot_vs_anchor"] = pivot / base_anchor_high

    if pivot <= final_contraction_low:
        r["reject"] = "진입 피봇이 마지막 수축 저점보다 낮음"
        r["reject_code"] = "pivot_below_low"
        return r
    # 9/11 수정(판호 지적 채택): 종가가 이미 피봇 위인 경우를 "탈락"으로 처리하던 것을 철회한다.
    # 이건 VCP 조건 불합격이 아니라 **상태**다 — 돌파가 이미 시작됐다는 뜻일 뿐이다.
    # 피봇을 "마지막 수축의 고가"로 바꾼 뒤로는 종가가 그 위에 있는 경우가 흔해졌고,
    # 이걸 전부 버리면 READY 구간이 "마지막 저점 이후 직전 고점을 넘기 전"이라는
    # 아주 좁은 창으로 쪼그라든다. 신호가 극단적으로 적었던 주원인으로 의심된다.
    # 어떻게 다룰지는 진입 방식이 정한다:
    #   - 장중 스탑매수(A): 이미 놓친 돌파이므로 신규 주문을 걸지 않는다.
    #   - 종가확인(B): 오히려 이게 신호 그 자체다.
    r["late_breakout"] = c > pivot

    # ── 4. 타이트함 / 변동성 / 거래량 ──────────────────────────────
    r10 = (max(high[i - 9: i + 1]) - min(low[i - 9: i + 1])) / c
    r5 = (max(high[i - 4: i + 1]) - min(low[i - 4: i + 1])) / c
    r["range10"], r["range5"] = r10, r5
    if r10 > p.range10_max:
        r["reject"] = f"최근10일 변동폭 과대({r10:.1%})"
        r["reject_code"] = "range10_wide"
        return r
    if r5 > p.range5_max:
        r["reject"] = f"최근5일 변동폭 과대({r5:.1%})"
        r["reject_code"] = "range5_wide"
        return r

    atr_s = _atr(high, low, close, p.atr_short, i)
    atr_l = _atr(high, low, close, p.atr_long, i)
    r["atr5"], r["atr20"] = atr_s, atr_l
    if atr_s is None or atr_l is None or atr_l == 0:
        r["reject"] = "ATR 계산 불가"
        r["reject_code"] = "atr_calc"
        return r
    r["atr_ratio_5_20"] = atr_s / atr_l   # 참고용 기록 (하드 조건 아님 — 아래 주석 참고)

    # 9/9 형배 발견: ATR5/ATR20을 하드 조건으로 쓰면 안 됨.
    # ATR20 구간 자체가 이미 수축 구간을 대부분 포함하고 있어서 분모가 먼저 작아지고,
    # 그 결과 교과서적인 VCP(수축 9%→6.7%→4.2%→2.4%, 최근5일 변동폭 1.0%)조차 비율이
    # 0.87까지밖에 안 떨어졌다. 판호가 제안한 0.75 임계값은 진짜 VCP를 탈락시킨다.
    # 대신 "베이스 시작 시점의 변동성 대비 지금 변동성"으로 측정한다 — 이게 수축을
    # 실제로 재는 방식이다.
    # 9/11 판호 제안 채택: 특정 하루의 ATR을 분모로 쓰면 그날의 변동성 스파이크에 휘둘린다.
    # 가격 정규화(NATR = ATR5/종가) 후 여러 날의 중앙값으로 비교한다.
    def _natr_median(center, span):
        vals = []
        for j in range(center - span + 1, center + 1):
            a = _atr(high, low, close, p.atr_short, j)
            if a and close[j]:
                vals.append(a / close[j])
        if not vals:
            return None
        vals.sort()
        return vals[len(vals) // 2]

    natr_base = _natr_median(base_high_idx, p.natr_median_span_base)
    natr_now = _natr_median(i, p.natr_median_span_now)
    r["natr_base"], r["natr_now"] = natr_base, natr_now
    if not natr_base or not natr_now:
        r["skipped"].append("atr_contraction")
    else:
        atr_contraction = natr_now / natr_base
        r["atr_contraction"] = atr_contraction
        if atr_contraction > p.atr_contraction_max:
            r["reject"] = f"변동성 수축 부족(베이스 시작 대비 {atr_contraction:.2f})"
            r["reject_code"] = "atr_contraction_weak"
            return r

    # 거래량: 최근 5일 vs 그 이전 20일 (구간이 겹치지 않게)
    v_recent = sum(vol[i - p.vol_short + 1: i + 1]) / p.vol_short
    lo_v = i - p.vol_short - p.vol_long + 1
    v_base = sum(vol[lo_v: i - p.vol_short + 1]) / p.vol_long
    r["vol5"], r["vol20_prior"] = v_recent, v_base
    if v_base <= 0:
        r["reject"] = "거래량 기준 계산 불가"
        r["reject_code"] = "vol_calc"
        return r
    vol_ratio = v_recent / v_base
    r["vol_ratio"] = vol_ratio
    if vol_ratio > p.vol_ratio_max:
        r["reject"] = f"거래량 고갈 부족(비율 {vol_ratio:.2f})"
        r["reject_code"] = "vol_not_dry"
        return r

    # ── 5. 손절 / 진입 상한 ───────────────────────────────────────
    buffer = max(p.stop_atr_mult * atr_s, c * p.stop_min_pct)
    stop = final_contraction_low - buffer
    r["stop_buffer"], r["structural_stop"] = buffer, stop

    if stop <= 0 or stop >= pivot:
        r["reject"] = "손절가 계산 이상"
        r["reject_code"] = "stop_calc"
        return r

    # 주문 한 번으로 "추격 +2% 제한"과 "구조적 리스크 5% 제한"을 동시에 만족시키는 상한
    max_entry = min(pivot * (1 + p.chase_limit), stop / (1 - p.max_structural_risk))
    r["max_entry"] = max_entry
    r["risk_at_pivot"] = (pivot - stop) / pivot

    if max_entry < pivot:
        r["reject"] = f"피봇 진입 시 리스크 과대({r['risk_at_pivot']:.1%})"
        r["reject_code"] = "risk_too_big"
        return r

    r["ready"] = True
    return r


# ─────────────────────── 진단 전용: 조건을 전부 독립 평가 ───────────────────────
def evaluate_gates(bars, i, p: Params = None):
    """evaluate_vcp는 첫 실패에서 바로 반환하므로 "뒤쪽 조건이 얼마나 걸러내는지"를 알 수 없다.
    이 함수는 조기 반환 없이 모든 조건을 독립적으로 평가해서 {조건명: True/False/None}을 준다.
    None = 선행 정보가 없어 평가 불가. 판정 로직을 바꾸지 않으며 오직 진단용이다.
    (9/11 판호 제안: 단독 통과율 / 조건부 통과율 / 이 조건 하나 때문에만 탈락한 건수)"""
    p = p or Params()
    high, low, close, vol = bars["high"], bars["low"], bars["close"], bars["volume"]
    g = {}
    if i < p.max_base + p.atr_long + 5:
        return None
    c = close[i]

    sma50 = _sma(close, p.sma_fast, i)
    sma150 = _sma(close, p.sma_slow, i)
    g["1_close_above_sma50"] = None if sma50 is None else c > sma50
    g["2_sma50_above_sma150"] = True if sma150 is None else sma50 > sma150
    if i + 1 >= p.high_52w_lookback:
        h52 = max(high[i - p.high_52w_lookback + 1: i + 1])
        g["3_near_52w_high"] = c >= h52 * p.high_52w_min_ratio
    else:
        g["3_near_52w_high"] = True

    lo_idx = i - p.max_base + 1
    seg_high = high[lo_idx: i + 1]
    b0 = lo_idx + seg_high.index(max(seg_high))
    anchor = high[b0]
    g["4_base_long_enough"] = (i - b0) >= p.min_base

    adv_lo = max(0, b0 - p.prior_advance_lookback)
    if adv_lo < b0:
        g["5_prior_advance"] = (anchor / min(low[adv_lo:b0]) - 1) >= p.prior_advance_min
    else:
        g["5_prior_advance"] = True

    depths, kept, run = [], [], anchor
    for j in _find_swing_lows(low, b0, i, p.swing_k):
        run = max(run, max(high[b0: j + 1]))
        d = (run - low[j]) / run
        if d >= p.min_contraction_depth:
            depths.append(d); kept.append(j)
    g["6_contraction_count"] = len(depths) >= p.min_contractions
    if len(depths) >= 2:
        ratios = [depths[k + 1] / depths[k] for k in range(len(depths) - 1)]
        g["7_contraction_tightening"] = all(x <= p.contraction_ratio_max for x in ratios)
    else:
        g["7_contraction_tightening"] = None

    g["8_range10_tight"] = (max(high[i - 9:i + 1]) - min(low[i - 9:i + 1])) / c <= p.range10_max
    g["9_range5_tight"] = (max(high[i - 4:i + 1]) - min(low[i - 4:i + 1])) / c <= p.range5_max

    def _nm(center, span):
        vs = []
        for j in range(center - span + 1, center + 1):
            a = _atr(high, low, close, p.atr_short, j)
            if a and close[j]:
                vs.append(a / close[j])
        if not vs:
            return None
        vs.sort(); return vs[len(vs) // 2]
    nb, nn = _nm(b0, p.natr_median_span_base), _nm(i, p.natr_median_span_now)
    g["10_atr_contraction"] = None if not (nb and nn) else (nn / nb) <= p.atr_contraction_max

    v_recent = sum(vol[i - p.vol_short + 1:i + 1]) / p.vol_short
    lo_v = i - p.vol_short - p.vol_long + 1
    v_base = sum(vol[lo_v:i - p.vol_short + 1]) / p.vol_long
    g["11_volume_dry"] = None if v_base <= 0 else (v_recent / v_base) <= p.vol_ratio_max

    if len(kept) >= 1:
        seg_lo = kept[-2] if len(kept) >= 2 else b0
        pivot = max(high[seg_lo: kept[-1] + 1])
        fcl = low[kept[-1]]
        atr_s = _atr(high, low, close, p.atr_short, i)
        if atr_s and pivot > fcl:
            stop = fcl - max(p.stop_atr_mult * atr_s, c * p.stop_min_pct)
            g["12_risk_within_limit"] = stop > 0 and (pivot - stop) / pivot <= p.max_structural_risk
        else:
            g["12_risk_within_limit"] = None
    else:
        g["12_risk_within_limit"] = None
    return g


# ─────────────── 진단 전용: 병목 조건의 대안 규칙들을 나란히 평가 ───────────────
def evaluate_variants(bars, i, p: Params = None):
    """9/11 진단에서 `수축이 매 구간 20%씩 좁아져야 한다`는 규칙의 단독 통과율이 1.83%로
    다른 조건보다 8배 이상 빡세고, 이 조건 하나 때문에만 탈락한 건이 158건(현 READY 50건의 3배)
    으로 나왔다. 규칙을 바꾸기 전에 '어떤 형태의 규칙이 몇 개를 살리는지'를 먼저 측정한다.

    반환: {규칙그룹: {대안이름: True/False/None}}. 판정 로직은 건드리지 않는다."""
    p = p or Params()
    high, low, close, vol = bars["high"], bars["low"], bars["close"], bars["volume"]
    if i < p.max_base + p.atr_long + 5:
        return None
    c = close[i]
    out = {"contraction_shape": {}, "volume_dry": {}, "atr_contraction": {}}

    lo_idx = i - p.max_base + 1
    seg_high = high[lo_idx: i + 1]
    b0 = lo_idx + seg_high.index(max(seg_high))
    anchor = high[b0]

    depths, run = [], anchor
    for j in _find_swing_lows(low, b0, i, p.swing_k):
        run = max(run, max(high[b0: j + 1]))
        d = (run - low[j]) / run
        if d >= p.min_contraction_depth:
            depths.append(d)

    if len(depths) >= 2:
        ratios = [depths[k + 1] / depths[k] for k in range(len(depths) - 1)]
        fof = depths[-1] / depths[0]
        out["contraction_shape"] = {
            "A_every_leg_080(현행)": all(x <= 0.80 for x in ratios),
            "B_every_leg_090": all(x <= 0.90 for x in ratios),
            "C_final_over_first_080": fof <= 0.80,
            "D_final_over_first_070": fof <= 0.70,
            "E_final_depth_under_5pct": depths[-1] <= 0.05,
            "F_no_shape_rule": True,
        }
    else:
        out["contraction_shape"] = {k: None for k in
                                    ["A_every_leg_080(현행)", "B_every_leg_090",
                                     "C_final_over_first_080", "D_final_over_first_070",
                                     "E_final_depth_under_5pct", "F_no_shape_rule"]}

    v_recent = sum(vol[i - p.vol_short + 1:i + 1]) / p.vol_short
    lo_v = i - p.vol_short - p.vol_long + 1
    v_base = sum(vol[lo_v:i - p.vol_short + 1]) / p.vol_long
    if v_base > 0:
        vr = v_recent / v_base
        out["volume_dry"] = {"A_070(현행)": vr <= 0.70, "B_080": vr <= 0.80,
                             "C_090": vr <= 0.90, "D_none": True}
    else:
        out["volume_dry"] = {k: None for k in ["A_070(현행)", "B_080", "C_090", "D_none"]}

    def _nm(center, span):
        vs = []
        for j in range(center - span + 1, center + 1):
            a = _atr(high, low, close, p.atr_short, j)
            if a and close[j]:
                vs.append(a / close[j])
        if not vs:
            return None
        vs.sort(); return vs[len(vs) // 2]
    nb, nn = _nm(b0, p.natr_median_span_base), _nm(i, p.natr_median_span_now)
    if nb and nn:
        ac = nn / nb
        out["atr_contraction"] = {"A_075(현행)": ac <= 0.75, "B_085": ac <= 0.85,
                                  "C_095": ac <= 0.95, "D_none": True}
    else:
        out["atr_contraction"] = {k: None for k in ["A_075(현행)", "B_085", "C_095", "D_none"]}
    return out
