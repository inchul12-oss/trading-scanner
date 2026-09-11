"""
VCP 백테스트 실행기 — GitHub Actions에서 돌아간다.

형배 작업공간과 인철님 맥북 모두 시세 사이트 접근이 막혀 있어서(9/11 실측),
실제 데이터를 쓰는 건 이 스크립트가 유일하다. 여기서만 인터넷을 쓴다.

동작
1. 나스닥트레이더 공개 파일로 미국 상장종목 목록을 만든다.
2. 이름/플래그 기반으로 ETF·워런트·우선주 등을 걸러 보통주만 남긴다.
3. yfinance로 일봉을 배치 다운로드하고 유동성 필터를 건다.
4. 같은 데이터에 대해 진입/청산 조합과 대조군을 전부 돌린다.
5. 결과와 데이터 커버리지를 JSON으로 남긴다.

주의: 성과 숫자보다 먼저 볼 것은 커버리지다. 몇 종목을 요청해서 몇 개가 실제로
데이터를 반환했는지 기록하지 않으면 생존편향의 크기를 알 수 없다.
"""
import io
import json
import os
import sys
import time
import urllib.request
from datetime import datetime, timezone

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from vcp_setup import (Params, evaluate_vcp, evaluate_gates,    # noqa: E402
                       evaluate_variants)
from vcp_backtest import (BTConfig, backtest_symbol,            # noqa: E402
                          backtest_simple_breakout, summarize,
                          backtest_matched_control, new_counters)

NASDAQ_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/nasdaqlisted.txt"
OTHER_LISTED = "https://www.nasdaqtrader.com/dynamic/SymDir/otherlisted.txt"

# 보통주가 아닌 것을 이름으로 걸러낸다. assetType/ETF 플래그만 믿으면 안 된다는 걸
# 알파밴티지 데이터에서 확인했다(ETF가 Stock으로 분류된 사례 존재).
NAME_EXCLUDE = (
    " warrant", " warrants", " right", " rights", " unit", " units",
    "preferred", "depositary", " notes", " note due", "% note",
    "debenture", " trust preferred", "closed end", "acquisition corp",
)
SYMBOL_EXCLUDE_CHARS = ("$", ".W", ".U", ".R", "-P-", "-W", "-U", "-R")

MAX_SYMBOLS = int(os.getenv("VCP_MAX_SYMBOLS", "300"))
PERIOD = os.getenv("VCP_PERIOD", "5y")
MIN_PRICE = float(os.getenv("VCP_MIN_PRICE", "5"))
MIN_DOLLAR_VOL = float(os.getenv("VCP_MIN_DOLLAR_VOL", "10000000"))
BATCH = int(os.getenv("VCP_BATCH", "60"))
RUN_DIAG = os.getenv("VCP_DIAG", "1") == "1"     # 조건별 탈락 진단 실행 여부
RUN_GRID = os.getenv("VCP_GRID", "0") == "1"     # 파라미터 조합 그리드 실행 여부
RUN_SWEEP = os.getenv("VCP_SWEEP", "0") == "1"   # 손절폭 상한 스윕 실행 여부


def fetch_text(url, tries=3):
    for k in range(tries):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Mozilla/5.0"})
            with urllib.request.urlopen(req, timeout=60) as resp:
                return resp.read().decode("utf-8", errors="replace")
        except Exception as e:
            print(f"  다운로드 실패({k+1}/{tries}) {url}: {e}")
            time.sleep(3 * (k + 1))
    return None


def build_universe():
    """나스닥트레이더 공개 파일 → 미국 보통주 심볼 목록."""
    out, stats = [], {"raw": 0, "after_flags": 0, "after_name": 0}
    for url, is_nasdaq in ((NASDAQ_LISTED, True), (OTHER_LISTED, False)):
        txt = fetch_text(url)
        if not txt:
            continue
        lines = [l for l in txt.splitlines() if l and not l.startswith("File Creation")]
        header = lines[0].split("|")
        idx = {h.strip(): n for n, h in enumerate(header)}
        for line in lines[1:]:
            f = line.split("|")
            if len(f) < len(header):
                continue
            stats["raw"] += 1
            sym = f[idx.get("Symbol", idx.get("ACT Symbol", 0))].strip()
            name = f[idx.get("Security Name", 1)].strip()
            test = f[idx["Test Issue"]].strip() if "Test Issue" in idx else "N"
            etf = f[idx["ETF"]].strip() if "ETF" in idx else "N"
            if test == "Y" or etf == "Y" or not sym:
                continue
            stats["after_flags"] += 1
            low = name.lower()
            if any(x in low for x in NAME_EXCLUDE):
                continue
            if any(x in sym for x in SYMBOL_EXCLUDE_CHARS):
                continue
            stats["after_name"] += 1
            out.append(sym.replace(".", "-"))     # yfinance 표기(BRK.B → BRK-B)
    return sorted(set(out)), stats


def to_bars(df):
    """yfinance DataFrame → 순수 파이썬 dict(백테스트 엔진 입력 형식)."""
    df = df.dropna()
    if len(df) < 300:
        return None
    return {
        "open": [float(x) for x in df["Open"]],
        "high": [float(x) for x in df["High"]],
        "low": [float(x) for x in df["Low"]],
        "close": [float(x) for x in df["Close"]],
        "volume": [float(x) for x in df["Volume"]],
        # 9/11 추가: 날짜를 같이 들고 간다.
        # 이게 없으면 (1) 포트폴리오 기준 최대낙폭을 못 구하고
        # (2) 전반부/후반부 분할 검증(판호 제안 4번)을 못 한다.
        "date": [str(x)[:10] for x in df.index],
    }


def main():
    import yfinance as yf
    import pandas as pd

    t0 = time.time()
    print("1) 유니버스 구성")
    universe, ustats = build_universe()
    print(f"   원본 {ustats['raw']} → 플래그필터 {ustats['after_flags']} → 이름필터 {ustats['after_name']}")
    if not universe:
        print("   유니버스 구성 실패 — 중단")
        json.dump({"error": "universe_empty"}, open("vcp_backtest_result.json", "w"))
        return
    universe = universe[:MAX_SYMBOLS]
    print(f"   이번 실행 대상: {len(universe)}종목 (상한 {MAX_SYMBOLS})")

    print(f"2) 일봉 다운로드 ({PERIOD})")
    bars_by_sym, cov = {}, {"requested": len(universe), "downloaded": 0,
                            "too_short": 0, "illiquid": 0, "failed": 0}
    for k in range(0, len(universe), BATCH):
        chunk = universe[k:k + BATCH]
        try:
            data = yf.download(chunk, period=PERIOD, interval="1d",
                               auto_adjust=False, group_by="ticker",
                               threads=False, progress=False)
        except Exception as e:
            print(f"   배치 실패 {k}: {e}")
            cov["failed"] += len(chunk)
            continue
        multi = isinstance(data.columns, pd.MultiIndex)
        for s in chunk:
            try:
                df = data[s] if multi else data
            except Exception:
                cov["failed"] += 1
                continue
            b = to_bars(df)
            if b is None:
                cov["too_short"] += 1
                continue
            px = b["close"][-1]
            adv = sum(b["close"][j] * b["volume"][j] for j in range(-20, 0)) / 20
            if px < MIN_PRICE or adv < MIN_DOLLAR_VOL:
                cov["illiquid"] += 1
                continue
            bars_by_sym[s] = b
            cov["downloaded"] += 1
        print(f"   {k + len(chunk)}/{len(universe)}  확보 {cov['downloaded']}")
        time.sleep(1)

    print(f"   커버리지: 요청 {cov['requested']} / 사용가능 {cov['downloaded']} "
          f"/ 데이터짧음 {cov['too_short']} / 유동성미달 {cov['illiquid']} / 실패 {cov['failed']}")
    if not bars_by_sym:
        json.dump({"error": "no_data", "coverage": cov}, open("vcp_backtest_result.json", "w"))
        return

    # ── 진단 (9/11, 판호 제안 방식) ────────────────────────────────────
    # A. 퍼널 통과율: 조건을 순서대로 적용할 때 어디서 절벽이 생기는가
    # B. 단독 통과율: 각 조건을 혼자 적용하면 몇 %가 통과하는가
    # C. 구조 카운트: "이 조건 하나 때문에만 탈락"한 건수 (판호가 rescue count라 부른 것)
    # 여기서는 어떤 임계값도 바꾸지 않는다. 오직 세기만 한다.
    print("2.5) 조건별 탈락 진단")
    p = Params()
    if not RUN_DIAG:
        print("   (VCP_DIAG=0 — 건너뜀)")
    funnel, solo_pass, solo_eval, rescue = {}, {}, {}, {}
    bars_evaluated = ready_bars = late_bk = 0
    ready_syms = set()
    # 대안 규칙별로 "그 규칙만 바꿨을 때 READY가 몇 개가 되는가"
    VAR_GATE = {"contraction_shape": "7_contraction_tightening",
                "volume_dry": "11_volume_dry",
                "atr_contraction": "10_atr_contraction"}
    ready_if = {grp: {} for grp in VAR_GATE}
    for s_, b in (bars_by_sym.items() if RUN_DIAG else []):
        n = len(b["close"])
        for i in range(p.max_base + p.atr_long + 6, n):
            try:
                r = evaluate_vcp(b, i, p)
                g = evaluate_gates(b, i, p)
            except Exception:
                funnel["ERROR"] = funnel.get("ERROR", 0) + 1
                continue
            bars_evaluated += 1
            if r.get("ready"):
                ready_bars += 1
                ready_syms.add(s_)
                funnel["READY"] = funnel.get("READY", 0) + 1
                if r.get("late_breakout"):
                    late_bk += 1
            else:
                funnel[r.get("reject_code", "unknown")] = funnel.get(r.get("reject_code", "unknown"), 0) + 1
            if g:
                fails = [k for k, v in g.items() if v is False]
                for k, v in g.items():
                    if v is None:
                        continue
                    solo_eval[k] = solo_eval.get(k, 0) + 1
                    if v:
                        solo_pass[k] = solo_pass.get(k, 0) + 1
                if len(fails) == 1:
                    rescue[fails[0]] = rescue.get(fails[0], 0) + 1
                try:
                    vv = evaluate_variants(b, i, p)
                except Exception:
                    vv = None
                if vv:
                    for grp, gate_name in VAR_GATE.items():
                        others_ok = all(v is not False for k, v in g.items() if k != gate_name)
                        for vname, vpass in vv.get(grp, {}).items():
                            ready_if[grp].setdefault(vname, 0)
                            if others_ok and vpass:
                                ready_if[grp][vname] += 1

    print(f"   평가 일봉 {bars_evaluated:,} / READY {ready_bars:,} "
          f"({ready_bars / max(bars_evaluated,1) * 100:.4f}%) / READY 종목 {len(ready_syms)} "
          f"/ 그중 이미돌파(late_breakout) {late_bk:,}")
    print("   [A] 순차 적용 시 최초 탈락 지점")
    for code, cnt in sorted(funnel.items(), key=lambda kv: -kv[1]):
        print(f"       {code:<28} {cnt:>9,}  ({cnt / max(bars_evaluated,1) * 100:6.2f}%)")
    print("   [D] 대안 규칙별 READY 수 (해당 규칙만 교체, 나머지 조건 동일)")
    for grp, dd in ready_if.items():
        print(f"       <{grp}>")
        for vname, cnt in sorted(dd.items(), key=lambda kv: -kv[1]):
            print(f"           {vname:<28} READY {cnt:>6,}")
    print("   [B] 조건 단독 통과율   [C] 이 조건 하나 때문에만 탈락(rescue)")
    for k in sorted(solo_eval):
        pr = solo_pass.get(k, 0) / solo_eval[k] * 100
        print(f"       {k:<28} 단독통과 {pr:6.2f}%   rescue {rescue.get(k, 0):>7,}")

    # ── 파라미터 조합 그리드 (9/11, 판호 권고 방식) ──────────────────────
    # 최고 수치 하나를 고르는 게 목적이 아니다. 주변 조합까지 함께 괜찮은 "고원"이
    # 있는지 보기 위한 것이다. 한 조합만 유독 튀면 그건 curve fitting 신호다.
    if RUN_GRID:
        print("3-G) 파라미터 그리드")
        grid = []
        for rule in ("every_leg", "final_over_first"):
            for vol in (0.70, 0.85):
                for atr in (0.75, 0.90):
                    for entry in ("stop_buy", "close_confirm"):
                        for exit_m in ("ema10", "ema20", "atr_trail"):
                            grid.append((rule, vol, atr, entry, exit_m))
        grid_results = {}
        for gi, (rule, vol, atr, entry, exit_m) in enumerate(grid, 1):
            pg = Params(contraction_rule=rule, vol_ratio_max=vol, atr_contraction_max=atr)
            cfg = BTConfig(entry_mode=entry, exit_mode=exit_m)
            trades = []
            for s_, b in bars_by_sym.items():
                try:
                    trades += backtest_symbol(s_, b, cfg, pg)
                except Exception:
                    pass
            key = f"{rule}|vol{vol}|atr{atr}|{entry}|{exit_m}"
            sm = summarize(trades)
            sm["distinct_symbols"] = len({t["symbol"] for t in trades})
            grid_results[key] = sm
            print(f"   [{gi}/{len(grid)}] {key}  n={sm['n']} "
                  f"exp={sm.get('expectancy_r')} PF={sm.get('profit_factor')}")
        out_grid = grid_results
    else:
        out_grid = None

    print("3) 백테스트")
    results, all_trades, all_counters = {}, {}, {}
    combos = [(e, x) for e in ("stop_buy", "close_confirm") for x in ("ema10", "ema20", "atr_trail")]
    for entry, exit_m in combos:
        cfg = BTConfig(entry_mode=entry, exit_mode=exit_m)
        trades, cnt = [], new_counters()
        for s, b in bars_by_sym.items():
            try:
                trades += backtest_symbol(s, b, cfg, p, counters=cnt)
            except Exception as e:
                print(f"   {s} 백테스트 오류: {e}")
        key = f"{entry}|{exit_m}"
        results[key] = summarize(trades)
        results[key]["distinct_symbols"] = len({t["symbol"] for t in trades})
        all_trades[key] = trades
        all_counters[key] = cnt
        print(f"   VCP {key}: {results[key]}")
        print(f"        손절폭게이트: {cnt}")

    print("4) 대조군 A — 원래 버전 (손절 = 진입가-2*ATR14, 리스크 제한 없음)")
    print("   ※ VCP와 R의 분모 정의가 달라서 R 직접 비교는 성립하지 않는다.")
    print("     거래당 평균 %수익률(avg_ret_pct)로만 비교할 것.")
    for exit_m in ("ema10", "ema20", "atr_trail"):
        cfg = BTConfig(exit_mode=exit_m)
        trades = []
        for s, b in bars_by_sym.items():
            try:
                trades += backtest_simple_breakout(s, b, cfg)
            except Exception as e:
                print(f"   {s} 대조군 오류: {e}")
        key = f"CONTROL|{exit_m}"
        results[key] = summarize(trades)
        results[key]["distinct_symbols"] = len({t["symbol"] for t in trades})
        all_trades[key] = trades
        print(f"   {key}: {results[key]}")

    print("5) 대조군 B — 매칭 버전 (손절·리스크제한·슬리피지·청산을 VCP와 완전 동일)")
    print("   ※ 이게 진짜 비교다. VCP 셋업 필터만 켜고 끈 차이만 남는다.")
    for exit_m in ("ema10", "ema20", "atr_trail"):
        cfg = BTConfig(exit_mode=exit_m)
        trades, cnt = [], new_counters()
        for s, b in bars_by_sym.items():
            try:
                trades += backtest_matched_control(s, b, cfg, p, counters=cnt)
            except Exception as e:
                print(f"   {s} 매칭대조군 오류: {e}")
        key = f"CONTROL_MATCHED|{exit_m}"
        results[key] = summarize(trades)
        results[key]["distinct_symbols"] = len({t["symbol"] for t in trades})
        all_trades[key] = trades
        all_counters[key] = cnt
        print(f"   {key}: {results[key]}")
        print(f"        손절폭게이트: {cnt}")

    # ── 6) 손절폭 상한 스윕 (9/11) ────────────────────────────────────
    # 9/11 실측: 매칭 대조군 신호 29,887건 중 27,591건(92.3%)이 "손절폭 5% 초과"로
    # 탈락했다. VCP 조건 전부를 합친 것보다 이 숫자 하나가 훨씬 크게 작용한다.
    # 5%는 근거 없이 정해둔 값이므로, 여기서 최적값을 고르려는 게 아니라
    # "우리가 이 규칙으로 무엇을 얼마나 버리고 있는지" 크기를 재는 것이다.
    sweep = {}
    if RUN_SWEEP:
        print("6) 손절폭 상한 스윕")
        for cap in (0.03, 0.05, 0.07, 0.10, 1.00):
            for label, fn in (("MATCHED", "m"), ("VCP", "v")):
                for exit_m in ("ema10", "ema20"):
                    cfg = BTConfig(entry_mode="close_confirm", exit_mode=exit_m,
                                   max_risk_pct=cap)
                    pc = Params(max_structural_risk=min(cap, 0.99))
                    trades, cnt = [], new_counters()
                    for s_, b in bars_by_sym.items():
                        try:
                            if fn == "m":
                                trades += backtest_matched_control(
                                    s_, b, cfg, pc, counters=cnt)
                            else:
                                trades += backtest_symbol(s_, b, cfg, pc, counters=cnt)
                        except Exception:
                            pass
                    key = f"{label}|cap{int(cap*100)}|{exit_m}"
                    sm = summarize(trades)
                    sm["distinct_symbols"] = len({t["symbol"] for t in trades})
                    sm["gate"] = cnt
                    sweep[key] = sm
                    print(f"   {key:<26} n={sm.get('n',0):<6} "
                          f"exp={sm.get('expectancy_r')} "
                          f"avg%={sm.get('avg_ret_pct')} "
                          f"MDD={sm.get('max_drawdown_r')} "
                          f"탈락(상한)={cnt.get('risk_above_cap')}")

    best = max((k for k in all_trades if not k.startswith("CONTROL")),
               key=lambda k: results[k].get("n", 0))

    # 거래 원본 전체를 따로 저장한다.
    # 지금까지는 요약만 남아서 뭔가 더 보고 싶을 때마다 6분짜리 실행을 다시 돌려야 했다.
    # 원본이 있으면 부트스트랩이든 구간별 분석이든 다시 돌리지 않고 바로 할 수 있다.
    with open("vcp_trades.json", "w") as f:
        json.dump({"run_at_utc": datetime.now(timezone.utc).isoformat(),
                   "counters": all_counters,
                   "trades": {k: v for k, v in all_trades.items()}},
                  f, ensure_ascii=False, separators=(",", ":"))
    print(f"   거래 원본 저장: vcp_trades.json "
          f"({sum(len(v) for v in all_trades.values()):,}건)")

    out = {
        "run_at_utc": datetime.now(timezone.utc).isoformat(),
        "elapsed_sec": round(time.time() - t0, 1),
        "params": {"period": PERIOD, "max_symbols": MAX_SYMBOLS,
                   "min_price": MIN_PRICE, "min_dollar_vol": MIN_DOLLAR_VOL},
        "universe_stats": ustats,
        "diagnostics": {
            "bars_evaluated": bars_evaluated,
            "ready_bars": ready_bars,
            "ready_symbols": len(ready_syms),
            "late_breakout_bars": late_bk,
            "funnel_first_fail": dict(sorted(funnel.items(), key=lambda kv: -kv[1])),
            "solo_pass_rate": {k: round(solo_pass.get(k, 0) / solo_eval[k], 4) for k in sorted(solo_eval)},
            "rescue_count": dict(sorted(rescue.items(), key=lambda kv: -kv[1])),
            "ready_if_rule_replaced": ready_if,
        },
        "coverage": cov,
        "risk_gate_counters": all_counters,
        "results": results,
        "grid": out_grid,
        "risk_cap_sweep": sweep or None,
        "sample_trades": all_trades[best][:40],
    }
    with open("vcp_backtest_result.json", "w") as f:
        json.dump(out, f, indent=2, ensure_ascii=False)
    print(f"\n완료 — {out['elapsed_sec']}초")


if __name__ == "__main__":
    main()
