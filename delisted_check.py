"""
delisted_check.py — 상장폐지 종목 yfinance 커버리지 테스트 [사전등록 동결본 v1]

■ 왜 하는가
  판호 슬레이트(개별주 전략) 전체가 PIT(시점기준) 데이터에 막혀 있다.
  실측 결과 2021-09-10 미국 보통주 5,724종목 중 1,329종목(23.2%)이 이후 소멸했고,
  오늘의 상장목록만 쓰면 5년 전 시장의 76.8%만 보게 된다.
  상폐 종목의 **보정된** 과거 주가를 확보할 수 있는지가 게이트다.
  (알파밴티지는 무보정만 무료, 하루 25회 제한 / Twelve Data는 상폐 종목 미보유)

■ 표본 (사전등록, 변경 금지)
  Alpha Vantage LISTING_STATUS(state=delisted) 전량 → 보통주 필터 4,445종목
  → 2021-01-01 ~ 2026-06-30 상폐 & 상장 후 400일 이상 생존 → 1,433종목
  → 연도별 층화 무작위 100종목, **시드 20260911 고정**
  결과가 나쁘다고 표본을 다시 뽑지 않는다.

■ 검사 5항목 (판호 지정)
  1) OHLCV 반환 여부 / 봉 개수
  2) 마지막 봉 날짜가 실제 상폐일까지 도달하는가 (일수 차이)
  3) auto_adjust=True 가 실제로 작동하는가 (보정/무보정 종가 대조)
  4) 배당·분할 이벤트 정보 존재 여부
  5) 이상구간 — 1일 -50% 이하 또는 +100% 이상 점프 / 끝부분 거래량 0 구간

■ 판정
  단순 성공/실패가 아니라 **delisted sample coverage %** 를 숫자로 남긴다.
  보통주 여부가 의심되는 심볼(5글자·하이픈 등 채권/우선주 관행)은 별도 표기하고
  커버리지를 "원본 기준"과 "의심 제외 기준" 두 가지로 보고한다.
"""

import json
import time
from datetime import date, datetime, timezone

# 심볼 -> 실제 상장폐지일 (Alpha Vantage LISTING_STATUS 기준)
SAMPLE_JSON = """
{"TNAV":"2021-02-17","AERO":"2021-02-26","OXFD":"2021-03-08","COWNZ":"2021-04-22",
"RBY":"2021-05-20","NEWP":"2021-05-21","GLOG":"2021-06-14","DTJ":"2021-06-29",
"BWL-A":"2021-08-16","SYKE":"2021-08-27","AEB":"2021-09-14","GPR":"2021-10-05",
"QADA":"2021-11-05","ZGYH":"2021-11-19","CAI":"2021-11-22","INOV":"2021-11-24",
"LMRK":"2021-12-22","STFC":"2022-03-01","DISCB":"2022-04-08","PSB":"2022-07-20",
"PBIP":"2022-09-07","SHI":"2022-09-07","TUFN":"2022-09-07","REGI":"2022-09-08",
"ZNGA":"2022-09-08","XENT":"2022-09-16","NMMC":"2022-09-30","LMAO":"2022-10-28",
"TMX":"2022-10-28","CTXS":"2022-11-02","BSKY":"2022-12-12","PCPC":"2022-12-14",
"EMCF":"2022-12-30","RZA":"2023-01-09","IBER":"2023-03-01","ALBO":"2023-03-02",
"FSTX":"2023-03-09","AGGR":"2023-03-10","LEGA":"2023-03-14","SMIH":"2023-03-16",
"AVYA":"2023-05-01","GBRGR":"2023-05-17","SIRE":"2023-05-26","GTXAP":"2023-06-12",
"HHR":"2023-06-20","BGCP":"2023-06-30","PCGU":"2023-08-15","GECCN":"2023-09-06",
"WMC":"2023-12-06","NM":"2023-12-14","GDNR":"2023-12-27","RPT":"2024-01-02",
"ESMT":"2024-01-25","NGMS":"2024-04-25","SWAV":"2024-05-31","TDCX":"2024-06-18",
"SCCB":"2024-06-28","WRK":"2024-07-05","ETRN":"2024-07-22","IFIN":"2024-08-21",
"HA":"2024-09-18","DOMA":"2024-09-27","AUGX":"2024-10-02","CHK":"2024-10-04",
"LLAP":"2024-10-30","TELLL":"2024-10-31","PRMW":"2024-11-08","THCP":"2024-12-10",
"AZPN":"2025-03-12","NVRO":"2025-04-02","SKGR":"2025-04-10","PYCR":"2025-04-14",
"ALLK":"2025-05-15","LSBK":"2025-07-18","VIGL":"2025-08-05","LUX":"2025-08-06",
"QUSA":"2025-08-06","STR":"2025-08-18","DNB":"2025-08-25","MAG":"2025-09-03",
"GMS":"2025-09-04","OLO":"2025-09-12","WOLF":"2025-09-26","BEDU":"2025-12-15",
"GES":"2026-01-22","RNA":"2026-02-26","RNAM":"2026-03-03","SGN":"2026-03-16",
"CMLSQ":"2026-03-20","HOLX":"2026-04-07","SWKH":"2026-04-07","SEE":"2026-04-09",
"UDMY":"2026-05-11","APLS":"2026-05-14","ECCX":"2026-05-19","MCW":"2026-05-19",
"CNGL":"2026-05-28","LTCH":"2026-05-28","LEGT":"2026-06-09","FCRX":"2026-06-12"}
"""

MIN_BARS_OK = 250          # 이 이상이면 "히스토리 확보" 로 본다
GAP_DAYS_OK = 10           # 마지막 봉이 상폐일로부터 이 일수 이내면 "끝까지 확보"


def suspicious(sym):
    """보통주가 아닐 가능성이 높은 심볼 표기(채권/우선주 관행). 제외가 아니라 표기용."""
    return len(sym) >= 5 or "-" in sym


def days_between(a, b):
    try:
        return abs((date.fromisoformat(a) - date.fromisoformat(b)).days)
    except Exception:
        return None


def check_one(sym, dl_date):
    import yfinance as yf
    rec = {"symbol": sym, "delisting_date": dl_date, "suspicious": suspicious(sym)}
    try:
        tk = yf.Ticker(sym)
        adj = tk.history(period="max", interval="1d", auto_adjust=True)
        raw = tk.history(period="max", interval="1d", auto_adjust=False)
    except Exception as e:
        rec["error"] = str(e)[:160]
        return rec

    n = 0 if adj is None else len(adj)
    rec["bars"] = n
    if n == 0:
        rec["result"] = "NO_DATA"
        return rec

    dates = [str(x)[:10] for x in adj.index]
    rec["first"], rec["last"] = dates[0], dates[-1]
    rec["gap_days_to_delisting"] = days_between(dates[-1], dl_date)

    # 3) auto_adjust 가 실제로 값을 바꾸는가
    adj_works = None
    if raw is not None and len(raw) == n:
        try:
            diffs = sum(1 for a, b in zip(adj["Close"], raw["Close"]) if abs(a - b) > 1e-9)
            rec["adj_diff_bars"] = diffs
            adj_works = diffs > 0
        except Exception:
            pass
    rec["auto_adjust_effective"] = adj_works

    # 4) 배당/분할 이벤트
    try:
        act = tk.actions
        rec["actions"] = 0 if act is None else int(len(act))
    except Exception:
        rec["actions"] = None

    # 5) 이상구간
    closes = [float(x) for x in adj["Close"]]
    vols = [float(x) for x in adj["Volume"]] if "Volume" in adj else []
    big_dn = big_up = 0
    for i in range(1, len(closes)):
        if closes[i - 1] > 0:
            r = closes[i] / closes[i - 1] - 1.0
            if r <= -0.5:
                big_dn += 1
            elif r >= 1.0:
                big_up += 1
    tail_zero = 0
    for v in reversed(vols):
        if v == 0:
            tail_zero += 1
        else:
            break
    rec["jump_down_50"] = big_dn
    rec["jump_up_100"] = big_up
    rec["tail_zero_volume_days"] = tail_zero

    enough = n >= MIN_BARS_OK
    reaches = rec["gap_days_to_delisting"] is not None and rec["gap_days_to_delisting"] <= GAP_DAYS_OK
    if not enough:
        rec["result"] = "SHORT"
    elif not reaches:
        rec["result"] = "TRUNCATED"       # 데이터는 있는데 상폐 직전까지 안 옴
    else:
        rec["result"] = "OK"
    return rec


def main():
    t0 = datetime.now(timezone.utc)
    sample = json.loads(SAMPLE_JSON)
    print(f"표본 {len(sample)}종목 (시드 20260911 고정, 사전등록)")

    recs = []
    for i, (sym, dl) in enumerate(sorted(sample.items(), key=lambda x: x[1]), 1):
        r = check_one(sym, dl)
        recs.append(r)
        print(f"  {i:3d}/{len(sample)} {sym:7s} {dl}  "
              f"{r.get('result', 'ERR'):9s} bars={r.get('bars', 0):5d} "
              f"last={r.get('last', '-')} gap={r.get('gap_days_to_delisting')} "
              f"adj={r.get('auto_adjust_effective')} act={r.get('actions')}")
        time.sleep(0.3)

    def cov(rows):
        n = len(rows)
        if n == 0:
            return {}
        okc = sum(1 for r in rows if r.get("result") == "OK")
        return {
            "n": n,
            "OK": okc,
            "TRUNCATED": sum(1 for r in rows if r.get("result") == "TRUNCATED"),
            "SHORT": sum(1 for r in rows if r.get("result") == "SHORT"),
            "NO_DATA": sum(1 for r in rows if r.get("result") == "NO_DATA"),
            "ERROR": sum(1 for r in rows if "error" in r),
            "coverage_pct": round(okc / n * 100, 1),
            "any_data_pct": round(sum(1 for r in rows if r.get("bars", 0) > 0) / n * 100, 1),
            "adj_effective_pct": round(
                sum(1 for r in rows if r.get("auto_adjust_effective")) / n * 100, 1),
            "actions_present_pct": round(
                sum(1 for r in rows if (r.get("actions") or 0) > 0) / n * 100, 1),
            "with_jump_down50": sum(1 for r in rows if (r.get("jump_down_50") or 0) > 0),
            "with_tail_zero_vol": sum(1 for r in rows if (r.get("tail_zero_volume_days") or 0) > 0),
        }

    clean = [r for r in recs if not r["suspicious"]]
    out = {
        "run_at_utc": t0.isoformat(),
        "preregistered": {"seed": 20260911, "n": len(sample),
                          "min_bars_ok": MIN_BARS_OK, "gap_days_ok": GAP_DAYS_OK},
        "coverage_all": cov(recs),
        "coverage_excl_suspicious": cov(clean),
        "records": recs,
        "elapsed_sec": (datetime.now(timezone.utc) - t0).total_seconds(),
    }
    with open("delisted_check_result.json", "w") as f:
        json.dump(out, f, indent=1, ensure_ascii=False, default=str)

    print("\n" + "=" * 70)
    for k, v in (("원본 기준", out["coverage_all"]),
                 ("비보통주 의심 제외", out["coverage_excl_suspicious"])):
        print(f"[{k}] n={v['n']}  **coverage {v['coverage_pct']}%**  "
              f"(OK {v['OK']} / 잘림 {v['TRUNCATED']} / 짧음 {v['SHORT']} / "
              f"무데이터 {v['NO_DATA']} / 에러 {v['ERROR']})")
        print(f"    데이터라도 있는 비율 {v['any_data_pct']}% / "
              f"auto_adjust 작동 {v['adj_effective_pct']}% / "
              f"배당·분할 정보 {v['actions_present_pct']}%")
        print(f"    -50% 이상 급락 포함 {v['with_jump_down50']}종목 / "
              f"끝부분 거래량0 구간 {v['with_tail_zero_vol']}종목")
    print(f"\n완료 ({out['elapsed_sec']:.0f}초). delisted_check_result.json 저장")


if __name__ == "__main__":
    main()
