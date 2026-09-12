"""
run_market_study.py — GitHub Actions 실행용 러너 [사전등록 동결본 v1]

market_study.py 의 순수 로직에 실데이터를 붙여 Research 1 / Research 2 를 실행하고
market_study_result.json 을 남긴다. 상수는 market_study.py 에 동결돼 있다.
"""

import json
import sys
from datetime import datetime, timezone

import market_study as M


def to_series(df):
    return {
        "date": [str(x)[:10] for x in df.index],
        "open": [float(x) for x in df["Open"]],
        "high": [float(x) for x in df["High"]],
        "low": [float(x) for x in df["Low"]],
        "close": [float(x) for x in df["Close"]],
    }


def fetch(tickers):
    import yfinance as yf
    out = {}
    for t in tickers:
        for attempt in range(3):
            try:
                df = yf.download(t, period="max", interval="1d",
                                 auto_adjust=True, progress=False, threads=False)
                if df is None or len(df) == 0:
                    raise RuntimeError("빈 데이터")
                if hasattr(df.columns, "nlevels") and df.columns.nlevels > 1:
                    df.columns = df.columns.get_level_values(0)
                df = df.dropna()
                out[t] = to_series(df)
                print(f"  {t}: {len(df)}봉  {out[t]['date'][0]} ~ {out[t]['date'][-1]}")
                break
            except Exception as e:
                print(f"  {t} 실패({attempt+1}/3): {e}")
        if t not in out:
            print(f"  [경고] {t} 확보 실패")
    return out


def align(series_by_sym, symbols, start=None):
    """모든 심볼이 값을 가진 날짜만 남긴 공통 달력."""
    sets = [set(series_by_sym[s]["date"]) for s in symbols if s in series_by_sym]
    if not sets:
        return [], {}
    common = set.intersection(*sets)
    dates = sorted(d for d in common if (start is None or d >= start))
    close_by_sym, open_by_sym = {}, {}
    for s in symbols:
        if s not in series_by_sym:
            continue
        idx = {d: i for i, d in enumerate(series_by_sym[s]["date"])}
        close_by_sym[s] = [series_by_sym[s]["close"][idx[d]] for d in dates]
        open_by_sym[s] = [series_by_sym[s]["open"][idx[d]] for d in dates]
    return dates, close_by_sym, open_by_sym


def run_arm(name, dates, close_by_sym, positions, results):
    rets = M.apply_positions(dates, close_by_sym, positions)
    rdates = dates[1:]
    exp = M.exposure_ratio(positions)
    blend = M.static_blend(dates, close_by_sym, exp)
    results[name] = {"rdates": rdates, "rets": rets, "exposure": exp, "blend": blend}
    return results[name]


def report_period(label, results, order, start, end, out):
    print(f"\n{'='*78}\n[{label}]  {start} ~ {end}\n{'='*78}")
    print(f"{'arm':10s} {'CAGR':>9s} {'MDD':>9s} {'MAR':>8s} {'vol':>9s} "
          f"{'노출':>7s} {'혼합MAR':>8s}  판정")

    bench_d, bench_r = M.slice_period(results["BH_SPY"]["rdates"],
                                      results["BH_SPY"]["rets"], start, end)
    bench = M.summarize(bench_r)
    block = {"benchmark": bench, "arms": {}, "ci_metric": "cagr"}

    for name in order:
        r = results[name]
        d, rr = M.slice_period(r["rdates"], r["rets"], start, end)
        if len(rr) < 200:
            print(f"{name:10s}  (표본 부족 {len(rr)}일)")
            continue
        s = M.summarize(rr)
        _, br = M.slice_period(r["rdates"], r["blend"], start, end)
        sb = M.summarize(br)
        ci = None
        v, reasons = "-", []
        if name != "BH_SPY":
            if len(bench_r) == len(rr):
                ci = M.boot_diff(d, rr, bench_r, "cagr")
                v, reasons = M.verdict(s, bench, ci)
            else:
                # 달력이 어긋나면 대응 비교가 성립하지 않는다. 조용히 자기자신과
                # 비교해 가짜 판정을 내지 않고 명시적으로 보류한다.
                v = "N/A"
                reasons = [f"벤치마크와 기간 불일치 {len(rr)} vs {len(bench_r)}"]
        print(f"{name:10s} {M.fmt(s['cagr'])} {M.fmt(s['mdd'])} "
              f"{(s['mar'] if s['mar'] is not None else float('nan')):8.2f} "
              f"{M.fmt(s['vol'])} {r['exposure']*100:6.1f}% "
              f"{(sb['mar'] if sb['mar'] is not None else float('nan')):8.2f}  {v}"
              + (f"  ({'; '.join(reasons)})" if reasons else ""))
        block["arms"][name] = {"stats": s, "blend_stats": sb,
                               "exposure": r["exposure"], "ci_cagr_vs_spy": ci,
                               "verdict": v, "reasons": reasons}
    out[label] = block
    return block


def run_universe(tag, symbols, raw, out, report_from=None):
    """한 유니버스에 대해 arm 5개를 돌리고 train/test/full 을 보고한다.

    ※ 워밍업 처리: 신호에는 SMA200(200일)과 126일 룩백이 필요하다. 모든 arm의
      시작 조건을 동일하게 맞추기 위해 데이터는 최대한 앞에서부터 정렬하되,
      **보고 시작일은 워밍업이 끝난 뒤**로 잡는다. 그래야 매수보유 arm만
      앞구간을 공짜로 먹는 일이 없다.
    """
    syms = [s for s in symbols if s in raw]
    if "SPY" not in syms or "QQQ" not in syms:
        return
    dates, close, _ = align(raw, syms, start=None)
    warm = M.SMA_LEN + 5
    if len(dates) < warm + 400:
        print(f"  [{tag}] 표본 부족 {len(dates)}일 — 건너뜀")
        return
    start = report_from or dates[warm]
    print(f"\n  [{tag}] 달력 {len(dates)}일 {dates[0]}~{dates[-1]} "
          f"({', '.join(syms)}) / 보고 시작 {start}")

    res = {}
    run_arm("BH_SPY", dates, close, [None] + ["SPY"] * (len(dates) - 1), res)
    run_arm("BH_QQQ", dates, close, [None] + ["QQQ"] * (len(dates) - 1), res)
    run_arm("A_GATE", dates, close,
            M.build_positions(dates, close, syms, True, False, "QQQ"), res)
    run_arm("B_RANK", dates, close,
            M.build_positions(dates, close, syms, False, True), res)
    run_arm("C_BOTH", dates, close,
            M.build_positions(dates, close, syms, True, True), res)
    order = ["BH_SPY", "BH_QQQ", "A_GATE", "B_RANK", "C_BOTH"]

    train_end = M.TRAIN_END if start < M.TRAIN_END else start
    report_period(f"{tag}_TRAIN", res, order, start, train_end, out)
    report_period(f"{tag}_TEST", res, order, M.TEST_START, "2100-01-01", out)
    report_period(f"{tag}_FULL", res, order, start, "2100-01-01", out)

    # FULL 기간 MDD 초과는 기각이 아니라 '역사적 feasibility 실패'로 별도 표시
    tb, fb = out.get(f"{tag}_TEST"), out.get(f"{tag}_FULL")
    if tb and fb:
        for a, v in tb["arms"].items():
            fm = fb["arms"].get(a, {}).get("stats", {}).get("mdd")
            v["full_mdd"] = fm
            v["full_mdd_feasible"] = (fm is not None and fm <= M.MDD_LIMIT)
            if fm is not None and fm > M.MDD_LIMIT:
                print(f"  [{tag}] {a}: FULL MDD {fm*100:.1f}% > 50% "
                      f"→ 역사적 feasibility 실패(TEST 판정과는 별개)")

    for lbl, st, en in ((f"{tag}_TEST", M.TEST_START, "2100-01-01"),
                        (f"{tag}_FULL", start, "2100-01-01")):
        d1, r1 = M.slice_period(res["C_BOTH"]["rdates"], res["C_BOTH"]["rets"], st, en)
        _, r2 = M.slice_period(res["A_GATE"]["rdates"], res["A_GATE"]["rets"], st, en)
        if len(r1) == len(r2) and len(r1) > 200:
            ci = M.boot_diff(d1, r1, r2, "cagr")
            out[lbl]["rank_adds_over_gate_ci_cagr"] = ci
            if ci:
                keep = "랭킹 유지" if ci["lo"] > 0 else "랭킹 폐기(추가가치 없음)"
                print(f"  [{lbl}] C_BOTH - A_GATE  CAGR차 95%CI "
                      f"[{ci['lo']*100:+.2f}%p, {ci['hi']*100:+.2f}%p]  -> {keep}")
    return res


def main():
    t0 = datetime.now(timezone.utc)
    out = {"run_at_utc": t0.isoformat(), "preregistered": {
        "sma_len": M.SMA_LEN, "lookback": M.LOOKBACK,
        "cost_one_way": M.COST_ONE_WAY, "slip_one_way": M.SLIP_ONE_WAY,
        "loc_limit_mult": M.LOC_LIMIT_MULT, "mdd_limit": M.MDD_LIMIT,
        "train": [M.TRAIN_START, M.TRAIN_END], "test_start": M.TEST_START,
        "boot_iters": M.BOOT_ITERS, "boot_seed": M.BOOT_SEED}}

    print("1) 데이터 수집")
    raw = fetch(M.TICKERS_CORE + M.TICKERS_DEF)
    if "SPY" not in raw or "QQQ" not in raw:
        json.dump({"error": "SPY/QQQ 확보 실패"}, open("market_study_result.json", "w"))
        sys.exit(1)

    # ─────── Research 1 ───────
    print("\n2) Research 1 — 지수 노출 타이밍 / 로테이션 분해")
    print("  ※ 유니버스를 3단으로 나눈다. 종목을 늘릴수록 공통 달력이 늦게 시작되고,")
    print("    IWM(2000-05)·GLD(2004-11) 때문에 2000~2002 스트레스 구간이 잘려나가기 때문.")
    print("    MDD 50% 검증에서 최악 구간을 빼면 검증이 아니므로 2자산부터 본다.")

    # (1) 주력: SPY+QQQ — QQQ 상장(1999-03)부터. 2000~2002 전 구간 포함
    run_universe("R1_2A", ["SPY", "QQQ"], raw, out)
    # (2) 확장: +IWM — 공통 달력 2000-05부터
    run_universe("R1_3A", ["SPY", "QQQ", "IWM"], raw, out)
    # (3) 대조군: +GLD, IEF — 공통 달력 2004-11부터. 방어자산 효과 확인용
    run_universe("R1_DEF", M.TICKERS_CORE + M.TICKERS_DEF, raw, out)

    # ─────── Research 2 ───────
    print(f"\n{'='*78}\n3) Research 2 — 오버나이트 / 장중 분해\n{'='*78}")
    r2out = {}
    for t in ("SPY", "QQQ"):
        s = raw[t]
        d, on, itd = M.overnight_intraday(s["date"], s["open"], s["high"], s["low"], s["close"])
        bh = [s["close"][i] / s["close"][i - 1] - 1.0
              for i in range(1, len(s["date"])) if s["close"][i - 1] > 0]
        entry = {}
        for seg, series, dd in (("overnight", on, d), ("intraday", itd, d),
                                ("buy_hold", bh, s["date"][1:])):
            full = M.summarize(series)
            _, te = M.slice_period(dd, series, M.TEST_START, "2100-01-01")
            entry[seg] = {"full": full, "test": M.summarize(te),
                          "autocorr": [M.autocorr(series, k) for k in range(1, 6)]}
            print(f"  {t} {seg:10s} full CAGR {M.fmt(full['cagr'])} "
                  f"MDD {M.fmt(full['mdd'])} vol {M.fmt(full['vol'])} "
                  f"자기상관1~5 " + " ".join(
                      f"{(x if x is not None else float('nan')):+.3f}"
                      for x in entry[seg]["autocorr"]))

        sd, sr, filled, skipped = M.overnight_strategy(s["date"], s["open"], s["close"])
        full = M.summarize(sr)
        tdates, tr = M.slice_period(sd, sr, M.TEST_START, "2100-01-01")
        bhd = s["date"][1:]
        ci = None
        _, bh_test = M.slice_period(bhd, bh, M.TEST_START, "2100-01-01")
        if len(tr) == len(bh_test) and len(tr) > 200:
            ci = M.boot_mar_diff(tdates, tr, bh_test)
        # 비용 스트레스는 보고용. 판정은 사전등록대로 1.0배로만 한다.
        stress = {}
        for mult in (0.0, 1.0, 1.5, 3.0):
            _, sr_m, _, _ = M.overnight_strategy(s["date"], s["open"], s["close"], mult)
            _, tr_m = M.slice_period(sd, sr_m, M.TEST_START, "2100-01-01")
            stress[f"x{mult}"] = {"full": M.summarize(sr_m), "test": M.summarize(tr_m)}

        entry["strategy_loc"] = {
            "full": full, "test": M.summarize(tr),
            "filled_days": filled, "skipped_days": skipped,
            "fill_rate": filled / max(filled + skipped, 1),
            "ci_mar_vs_buyhold_test": ci,
            "cost_stress": stress}
        print(f"  {t} LOC오버나이트 전략  full CAGR {M.fmt(full['cagr'])} "
              f"MDD {M.fmt(full['mdd'])}  체결 {filled}일 / 미체결 {skipped}일 "
              f"({filled/max(filled+skipped,1)*100:.1f}% 체결)")
        print("      비용스트레스 full CAGR  " + "  ".join(
            f"{k}:{M.fmt(v['full']['cagr'])}" for k, v in stress.items())
            + "   (판정은 x1.0 기준)")
        if ci:
            print(f"      test MAR차 vs 매수보유 95%CI [{ci['lo']:+.3f}, {ci['hi']:+.3f}]")
        r2out[t] = entry
    out["R2"] = r2out

    out["elapsed_sec"] = (datetime.now(timezone.utc) - t0).total_seconds()
    with open("market_study_result.json", "w") as f:
        json.dump(out, f, indent=1, ensure_ascii=False, default=str)
    print(f"\n완료. market_study_result.json 저장 ({out['elapsed_sec']:.0f}초)")


if __name__ == "__main__":
    main()
