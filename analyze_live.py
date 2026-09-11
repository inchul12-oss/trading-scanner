"""
운영 중인 단타 시스템 검증 1단계 — "스캐너2가 고른 것 vs 안 고른 것".

질문 (판호 제안):
  스캐너2가 후보 중 일부만 진입신호로 골랐다. 고른 것이 안 고른 것보다
  실제로 더 잘 올랐는가? 아니라면 그 필터는 일을 안 하고 있거나 해를 끼친다.

설계 (결과 보기 전 고정):
  기준시각 T0 = 그 종목이 그날 스캐너2 목록에 처음 등장한 시각
  T0 이후 첫 5분봉 종가를 P0으로 잡고 +15/30/60분과 장마감까지의
  수익률 / MFE(최고) / MAE(최저)를 잰다.
  A군 = 그날 한 번이라도 entry_signal이 뜬 종목
  B군 = 끝까지 안 뜬 종목

왜 베타 보정을 여기서는 안 하나:
  A군과 B군을 "같은 날 같은 시각"에서 비교하므로 시장 움직임이 양쪽에
  공통으로 들어간다. 두 군의 차이에서는 대부분 상쇄된다.
  (절대 수익률을 평가할 때는 베타 보정이 필수지만, 여기 질문은 상대 비교다)

한계 (미리 명시):
  거래일 10일, A군 36건 중 20건이 9/3 하루에 몰려 있다.
  이건 유의성 검정이 아니라 방향 관찰이다. p값을 내지 않는다.
"""
import csv
import json
import sys
from datetime import datetime, timedelta, timezone

HORIZONS = [15, 30, 60]


def main():
    import yfinance as yf
    import pandas as pd

    rows = list(csv.reader(open("live_candidates.csv")))
    print(f"후보 {len(rows)}건")
    syms = sorted({r[1] for r in rows})
    days = sorted({r[0] for r in rows})
    print(f"고유종목 {len(syms)} / 거래일 {len(days)} ({days[0]} ~ {days[-1]})")

    start = (datetime.fromisoformat(days[0]) - timedelta(days=2)).date()
    end = (datetime.fromisoformat(days[-1]) + timedelta(days=2)).date()

    bars = {}
    B = 40
    for k in range(0, len(syms), B):
        chunk = syms[k:k + B]
        try:
            df = yf.download(chunk, start=str(start), end=str(end), interval="5m",
                             auto_adjust=False, group_by="ticker", threads=False,
                             progress=False, prepost=False)
        except Exception as e:
            print(f"  배치 실패 {k}: {e}")
            continue
        multi = isinstance(df.columns, pd.MultiIndex)
        for s in chunk:
            try:
                d = df[s] if multi else df
                d = d.dropna()
                if len(d) < 10:
                    continue
                idx = d.index
                if idx.tz is None:
                    idx = idx.tz_localize("UTC")
                else:
                    idx = idx.tz_convert("UTC")
                bars[s] = {
                    "t": [x.to_pydatetime() for x in idx],
                    "o": [float(x) for x in d["Open"]],
                    "h": [float(x) for x in d["High"]],
                    "l": [float(x) for x in d["Low"]],
                    "c": [float(x) for x in d["Close"]],
                }
            except Exception:
                continue
        print(f"  {k + len(chunk)}/{len(syms)} 확보 {len(bars)}")

    out, miss = [], 0
    for r in rows:
        day, sym, n, sig, gate, trig, vol, first, last, px0 = r
        b = bars.get(sym)
        if not b:
            miss += 1
            continue
        t0 = datetime.fromisoformat(f"{day}T{first}+00:00")
        # T0 이후 첫 봉
        i0 = None
        for i, t in enumerate(b["t"]):
            if t >= t0 and t.date().isoformat() == day:
                i0 = i
                break
        if i0 is None:
            miss += 1
            continue
        p0 = b["c"][i0]
        if p0 <= 0:
            miss += 1
            continue
        rec = {"day": day, "sym": sym, "sig": int(sig) > 0, "gate": int(gate) > 0,
               "n_snap": int(n), "p0": round(p0, 4),
               "t0": b["t"][i0].isoformat()}
        # 그날 마지막 봉 인덱스
        last_i = i0
        for i in range(i0, len(b["t"])):
            if b["t"][i].date().isoformat() != day:
                break
            last_i = i
        for H in HORIZONS:
            end_t = b["t"][i0] + timedelta(minutes=H)
            j = i0
            for i in range(i0, last_i + 1):
                if b["t"][i] <= end_t:
                    j = i
                else:
                    break
            seg_h = max(b["h"][i0 + 1:j + 1]) if j > i0 else b["h"][i0]
            seg_l = min(b["l"][i0 + 1:j + 1]) if j > i0 else b["l"][i0]
            rec[f"ret{H}"] = round(b["c"][j] / p0 - 1, 5)
            rec[f"mfe{H}"] = round(seg_h / p0 - 1, 5)
            rec[f"mae{H}"] = round(seg_l / p0 - 1, 5)
        rec["ret_eod"] = round(b["c"][last_i] / p0 - 1, 5)
        rec["mfe_eod"] = round(max(b["h"][i0:last_i + 1]) / p0 - 1, 5)
        rec["mae_eod"] = round(min(b["l"][i0:last_i + 1]) / p0 - 1, 5)
        rec["bars_in_day"] = last_i - i0 + 1
        out.append(rec)

    print(f"분석 가능 {len(out)}건 / 데이터 없음 {miss}건")

    def stat(rs, k):
        v = sorted(x[k] for x in rs if k in x)
        if not v:
            return None
        return {"n": len(v), "mean": round(sum(v) / len(v) * 100, 3),
                "med": round(v[len(v) // 2] * 100, 3),
                "p25": round(v[len(v) // 4] * 100, 3),
                "p75": round(v[3 * len(v) // 4] * 100, 3),
                "pos%": round(sum(1 for x in v if x > 0) / len(v) * 100, 1)}

    A = [x for x in out if x["sig"]]
    Bg = [x for x in out if not x["sig"]]
    GA = [x for x in out if x["gate"]]
    GB = [x for x in out if not x["gate"]]
    summary = {"n_A_signal": len(A), "n_B_nosignal": len(Bg),
               "n_gate_pass": len(GA), "n_gate_fail": len(GB), "groups": {}}
    for name, g in (("A_signal", A), ("B_nosignal", Bg),
                    ("gate_pass", GA), ("gate_fail", GB)):
        summary["groups"][name] = {k: stat(g, k) for k in
                                   [f"ret{H}" for H in HORIZONS] +
                                   [f"mfe{H}" for H in HORIZONS] +
                                   [f"mae{H}" for H in HORIZONS] +
                                   ["ret_eod", "mfe_eod", "mae_eod"]}
    # 날짜별로도 나눈다 — 9/3 한 날이 A군의 절반이라 전체 평균이 그날에 끌려간다
    byday = {}
    for d in sorted({x["day"] for x in out}):
        da = [x for x in out if x["day"] == d and x["sig"]]
        db = [x for x in out if x["day"] == d and not x["sig"]]
        byday[d] = {"nA": len(da), "nB": len(db),
                    "A_ret30": (stat(da, "ret30") or {}).get("med"),
                    "B_ret30": (stat(db, "ret30") or {}).get("med"),
                    "A_mfe60": (stat(da, "mfe60") or {}).get("med"),
                    "B_mfe60": (stat(db, "mfe60") or {}).get("med")}
    summary["by_day"] = byday

    json.dump({"summary": summary, "rows": out}, open("live_analysis.json", "w"),
              indent=1, ensure_ascii=False)
    print(json.dumps(summary["groups"]["A_signal"]["ret30"], ensure_ascii=False))
    print(json.dumps(summary["groups"]["B_nosignal"]["ret30"], ensure_ascii=False))
    print("완료 → live_analysis.json")


if __name__ == "__main__":
    main()
