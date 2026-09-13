"""스캐너1 눌림목 — 백테스트 확정 운용안 (20종목 / 60거래일 / 점수 상위)
매일 실행: 보유 점검 → 청산 알림 → 빈 자리만큼 신규 후보 → 텔레그램
인철님이 방에 '매수 XXX' / '매도 XXX' 남기면 자동으로 잡아서 기록한다.
필요 환경변수: TELEGRAM_BOT_TOKEN
"""
import json, os, re, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta
import numpy as np, pandas as pd, yfinance as yf

KST = timezone(timedelta(hours=9))
CHAT_ID = "-5569815780"
UNIV_FILE, POS_FILE = "sp500_current.txt", "positions.json"
SLOTS, HOLD_DAYS, STOP_PCT = 20, 60, 0.15
MIN_M6, MAX_A20, MIN_A50 = 0.30, 0.12, -0.08
PULL_RSI2, PULL_D5, PULL_A20 = 25, -0.04, -0.02
SHOW_MIN = 5


def wilder_rsi(c, n=2):
    c = np.asarray(c, float); d = np.diff(c)
    up = np.where(d > 0, d, 0.0); dn = np.where(d < 0, -d, 0.0)
    au = np.full(len(c), np.nan); ad = np.full(len(c), np.nan)
    if len(d) < n: return au
    au[n] = up[:n].mean(); ad[n] = dn[:n].mean()
    for i in range(n + 1, len(c)):
        au[i] = (au[i-1]*(n-1) + up[i-1]) / n
        ad[i] = (ad[i-1]*(n-1) + dn[i-1]) / n
    return 100 - 100/(1 + au/np.where(ad == 0, 1e-12, ad))


def tg(method, **kw):
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not tok: return None
    try:
        req = urllib.request.Request("https://api.telegram.org/bot%s/%s" % (tok, method),
                                     data=urllib.parse.urlencode(kw).encode())
        with urllib.request.urlopen(req, timeout=30) as r:
            return json.loads(r.read().decode())
    except Exception as e:
        print("텔레그램 %s 실패:" % method, repr(e)[:150]); return None


def load_state():
    if os.path.exists(POS_FILE):
        try: return json.load(open(POS_FILE))
        except Exception: pass
    return {"last_update_id": 0, "positions": [], "closed": []}


def read_orders(state):
    """방에 남긴 '매수 XXX' / '매도 XXX' 를 읽어온다"""
    got = {"buy": [], "sell": []}
    r = tg("getUpdates", offset=state.get("last_update_id", 0) + 1, timeout=0)
    if not r or not r.get("ok"): return got
    for u in r.get("result", []):
        state["last_update_id"] = max(state.get("last_update_id", 0), u.get("update_id", 0))
        m = u.get("message") or u.get("channel_post") or {}
        txt = (m.get("text") or "").strip()
        if not txt: continue
        for kw, key in (("매수", "buy"), ("매도", "sell")):
            if txt.startswith(kw):
                body = txt[len(kw):].strip()
                if body in ("없음", "없다", "패스", "pass", ""): continue
                got[key] += [x.upper() for x in re.findall(r"[A-Za-z][A-Za-z.\-]{0,6}", body)]
    return got


def main():
    syms = [x.strip().upper() for line in open(UNIV_FILE)
            if not line.strip().startswith("#") for x in line.split(",") if x.strip()]
    print("유니버스 %d종목" % len(syms))
    frames = {}; t0 = time.time()
    for i in range(0, len(syms), 100):
        b = syms[i:i+100]
        d = yf.download(b, period="2y", interval="1d", auto_adjust=False,
                        group_by="ticker", threads=True, progress=False)
        for s in b:
            try:
                sub = d[s][["Open", "High", "Low", "Close"]].dropna()
                if len(sub) > 250: frames[s] = sub
            except Exception: pass
        print("  batch %d: %d종목 %.0fs" % (i//100+1, len(frames), time.time()-t0), flush=True)
    if len(frames) < len(syms) * 0.8:
        print("수신 실패 과다 - 중단"); sys.exit(1)
    ref = frames.get("AAPL") or next(iter(frames.values()))
    asof = str(sorted(ref.index)[-1].date())
    print("기준일 %s" % asof)

    state = load_state()
    orders = read_orders(state)
    held = set(p["t"] for p in state["positions"])

    added = []
    for t in orders["buy"]:
        if t in held or t not in frames: continue
        state["positions"].append({"t": t, "signal_date": asof, "entry_px": None,
                                   "entry_date": None, "stop": None})
        held.add(t); added.append(t)

    for p in state["positions"]:
        if p["entry_px"] is None and p["t"] in frames:
            df = frames[p["t"]]
            nxt = df[df.index > pd.Timestamp(p["signal_date"])]
            if len(nxt):
                p["entry_date"] = str(nxt.index[0].date())
                p["entry_px"] = round(float(nxt.Open.iloc[0]), 4)
                p["stop"] = round(p["entry_px"] * (1 - STOP_PCT), 4)

    alerts = []; keep = []
    for p in state["positions"]:
        t = p["t"]
        if t in orders["sell"]:
            px = float(frames[t].Close.iloc[-1]) if t in frames else (p["entry_px"] or 0)
            r = ((px/p["entry_px"]-1)*100) if p["entry_px"] else 0.0
            alerts.append("[수동매도] %s  %+.1f%%" % (t, r))
            p["exit_date"] = asof; p["exit_px"] = px; p["why"] = "수동"; state["closed"].append(p); continue
        if t not in frames or p["entry_px"] is None: keep.append(p); continue
        df = frames[t]; after = df[df.index >= pd.Timestamp(p["entry_date"])]
        held_days = max(0, len(after) - 1)
        low_min = float(after.Low.min()) if len(after) else float("inf")
        px = float(df.Close.iloc[-1])
        if low_min <= p["stop"]:
            alerts.append("[손절] %s  진입 ${:,.2f} -> 손절선 ${:,.2f} 이탈 (%d일차)".replace("{:,.2f}","%.2f") % (t, p["entry_px"], p["stop"], held_days))
            p["exit_date"] = asof; p["exit_px"] = p["stop"]; p["why"] = "손절"; state["closed"].append(p)
        elif held_days >= HOLD_DAYS:
            alerts.append("[만기청산] %s  진입 $%.2f -> 현재 $%.2f  (%+.1f%%, %d일)"
                          % (t, p["entry_px"], px, (px/p["entry_px"]-1)*100, held_days))
            p["exit_date"] = asof; p["exit_px"] = px; p["why"] = "만기"; state["closed"].append(p)
        else:
            p["_d"] = held_days; p["_px"] = px; keep.append(p)
    live = keep
    state["positions"] = [dict((k, v) for k, v in p.items() if not k.startswith("_")) for p in keep]

    rows = []
    for s, df in frames.items():
        c = df.Close.to_numpy(float); h = df.High.to_numpy(float)
        if len(c) < 260: continue
        px = float(c[-1])
        rows.append(dict(t=s, px=px, m6=px/c[-127]-1, m3=px/c[-64]-1,
                         a20=px/c[-20:].mean()-1, a50=px/c[-50:].mean()-1, a200=px/c[-200:].mean()-1,
                         rsi2=float(wilder_rsi(c, 2)[-1]), d5=px/c[-6]-1,
                         frhi=px/float(h[-252:].max())-1))
    d = pd.DataFrame(rows)
    ok = ((d.m6 >= MIN_M6) & (d.a200 > 0) & (d.a50 > MIN_A50) & (d.a20 <= MAX_A20) &
          ((d.rsi2 <= PULL_RSI2) | (d.d5 <= PULL_D5) | (d.a20 <= PULL_A20)))
    cand = d[ok & ~d.t.isin(held)].copy()
    if len(cand):
        cand["score"] = (cand.m6.rank(pct=True)*0.5 + (-cand.rsi2).rank(pct=True)*0.3
                         + (-cand.a20).rank(pct=True)*0.2)
        cand = cand.sort_values("score", ascending=False)
        cand["stop"] = cand.px * (1 - STOP_PCT)
    free = max(0, SLOTS - len(live))
    show = cand.head(max(free, SHOW_MIN) if free else SHOW_MIN) if len(cand) else cand

    L = ["눌림목 - %s 종가" % asof]
    if alerts: L += ["", "[정리할 것]"] + ["  " + x for x in alerts]
    L += ["", "[보유 %d/%d종목]" % (len(live), SLOTS)]
    if live:
        for p in sorted(live, key=lambda x: -x.get("_d", 0)):
            if p["entry_px"]:
                L.append("  %-6s $%9.2f  %+6.1f%%  %d/%d일  손절 $%.2f"
                         % (p["t"], p["_px"], (p["_px"]/p["entry_px"]-1)*100, p["_d"], HOLD_DAYS, p["stop"]))
            else:
                L.append("  %-6s 진입가 확인중" % p["t"])
    else:
        L.append("  없음")
    L += ["", "[신규 후보] 빈자리 %d개" % free]
    if not len(show):
        L.append("  오늘은 조건 통과 종목 없음")
    else:
        if free == 0: L.append("  ※ 자리 없음 - 참고용")
        n = 0
        for _, x in show.iterrows():
            n += 1
            L.append("  %d. %s  $%.2f  손절 $%.2f" % (n, x["t"], x["px"], x["stop"]))
            L.append("     6개월 %+.0f%%  RSI2 %.0f  20일선 %+.1f%%  52주고점 %.0f%%"
                     % (x["m6"]*100, x["rsi2"], x["a20"]*100, x["frhi"]*100))
    if added: L += ["", "기록됨: 매수 " + ", ".join(added)]
    L += ["", "산 종목은 '매수 XXX', 판 종목은 '매도 XXX' 로 남기면 자동 기록"]
    msg = "\n".join(L)
    print(msg)
    tg("sendMessage", chat_id=CHAT_ID, text=msg)

    state["asof"] = asof
    state["updated_kst"] = datetime.now(KST).isoformat(timespec="seconds")
    json.dump(state, open(POS_FILE, "w"), ensure_ascii=False, indent=1)
    os.makedirs("log", exist_ok=True)
    if len(cand):
        cand.assign(rank=range(1, len(cand)+1)).to_csv("log/cand_%s.csv" % asof, index=False)


if __name__ == "__main__":
    main()
