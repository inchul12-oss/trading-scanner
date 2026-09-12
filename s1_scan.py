"""스캐너1 눌림목 - 동결 규칙(RULE_S1.md)
yfinance로 S&P500 일봉을 받아 조건 통과 종목을 뽑고 텔레그램으로 보낸다.
필요한 환경변수: TELEGRAM_BOT_TOKEN
"""
import json, os, sys, time, urllib.parse, urllib.request
from datetime import datetime, timezone, timedelta
import numpy as np, pandas as pd, yfinance as yf

KST = timezone(timedelta(hours=9))
CHAT_ID = "-5569815780"
UNIVERSE_FILE = "sp500_current.txt"

# 동결 규칙 파라미터 (RULE_S1.md - 결과 보고 바꾸지 말 것)
MIN_M6    = 0.30
MAX_A20   = 0.12
MIN_A50   = -0.08
PULL_RSI2 = 25
PULL_D5   = -0.04
PULL_A20  = -0.02
STOP_PCT  = 0.15


def wilder_rsi(close, n=2):
    c = np.asarray(close, float)
    d = np.diff(c)
    up = np.where(d > 0, d, 0.0)
    dn = np.where(d < 0, -d, 0.0)
    au = np.full(len(c), np.nan); ad = np.full(len(c), np.nan)
    if len(d) < n:
        return au
    au[n] = up[:n].mean(); ad[n] = dn[:n].mean()
    for i in range(n + 1, len(c)):
        au[i] = (au[i - 1] * (n - 1) + up[i - 1]) / n
        ad[i] = (ad[i - 1] * (n - 1) + dn[i - 1]) / n
    rs = au / np.where(ad == 0, 1e-12, ad)
    return 100 - 100 / (1 + rs)


def wilder_atr(h, l, c, n=20):
    h = np.asarray(h, float); l = np.asarray(l, float); c = np.asarray(c, float)
    tr = np.maximum(h[1:] - l[1:], np.maximum(abs(h[1:] - c[:-1]), abs(l[1:] - c[:-1])))
    out = np.full(len(c), np.nan)
    if len(tr) < n:
        return out
    out[n] = tr[:n].mean()
    for i in range(n + 1, len(c)):
        out[i] = (out[i - 1] * (n - 1) + tr[i - 1]) / n
    return out


def load_universe():
    syms = []
    with open(UNIVERSE_FILE) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            syms += [x.strip().upper() for x in line.split(",") if x.strip()]
    return syms


def fetch(syms):
    frames = {}; t0 = time.time()
    for i in range(0, len(syms), 100):
        b = syms[i:i + 100]
        d = yf.download(b, period="2y", interval="1d", auto_adjust=False,
                        group_by="ticker", threads=True, progress=False)
        for s in b:
            try:
                sub = d[s][["Open", "High", "Low", "Close"]].dropna()
                if len(sub) > 250:
                    frames[s] = sub
            except Exception:
                pass
        print("  batch %d: 누적 %d종목  %.0fs" % (i // 100 + 1, len(frames), time.time() - t0), flush=True)
    return frames


def scan(frames):
    rows = []
    for s, df in frames.items():
        c = df.Close.to_numpy(float); h = df.High.to_numpy(float); l = df.Low.to_numpy(float)
        if len(c) < 260:
            continue
        px = float(c[-1])
        rows.append(dict(
            t=s, px=px,
            m6=px / c[-127] - 1, m3=px / c[-64] - 1,
            a20=px / c[-20:].mean() - 1, a50=px / c[-50:].mean() - 1, a200=px / c[-200:].mean() - 1,
            rsi2=float(wilder_rsi(c, 2)[-1]), d5=px / c[-6] - 1,
            frhi=px / float(h[-252:].max()) - 1,
            atrp=float(wilder_atr(h, l, c, 20)[-1]) / px))
    d = pd.DataFrame(rows)
    ok = ((d.m6 >= MIN_M6) & (d.a200 > 0) & (d.a50 > MIN_A50) & (d.a20 <= MAX_A20) &
          ((d.rsi2 <= PULL_RSI2) | (d.d5 <= PULL_D5) | (d.a20 <= PULL_A20)))
    cand = d[ok].copy()
    if len(cand):
        cand["score"] = (cand.m6.rank(pct=True) * 0.5 + (-cand.rsi2).rank(pct=True) * 0.3
                         + (-cand.a20).rank(pct=True) * 0.2)
        cand = cand.sort_values("score", ascending=False)
        cand["stop"] = cand.px * (1 - STOP_PCT)
    return d, cand


def build_message(asof, n_univ, cand):
    L = ["눌림목 후보 - %s 종가 기준" % asof, "S&P500 %d개 중 %d개" % (n_univ, len(cand))]
    if not len(cand):
        L.append("오늘은 조건 통과 종목 없음")
    else:
        L.append("")
        for i, (_, x) in enumerate(cand.head(15).iterrows(), 1):
            L.append("%d. %s  $%,.2f  (손절 $%,.2f)".replace("%,", "%") % (i, x.t, x.px, x["stop"]))
            L.append("    6개월 %+.0f%% / 3개월 %+.0f%% / 52주고점 %.0f%%" % (x.m6 * 100, x.m3 * 100, x.frhi * 100))
            L.append("    RSI2 %.0f / 5일 %+.1f%% / 20일선 %+.1f%% / 변동성 %.1f%%"
                     % (x.rsi2, x.d5 * 100, x.a20 * 100, x.atrp * 100))
        if len(cand) > 15:
            L.append("... 외 %d개" % (len(cand) - 15))
    L += ["", "손절 -15% 고정, 물타기 없음. 보유 1~3개월",
          "산 종목은 이 방에 '매수 XXX YYY' 로 남겨두기"]
    return "\n".join(L)


def send(text):
    tok = os.environ.get("TELEGRAM_BOT_TOKEN")
    if not tok:
        print("TELEGRAM_BOT_TOKEN 없음 - 전송 생략"); return
    url = "https://api.telegram.org/bot%s/sendMessage" % tok
    data = urllib.parse.urlencode({"chat_id": CHAT_ID, "text": text}).encode()
    try:
        with urllib.request.urlopen(urllib.request.Request(url, data=data), timeout=30) as r:
            print("텔레그램 전송:", r.status)
    except Exception as e:
        print("텔레그램 실패:", repr(e)[:200])


def main():
    syms = load_universe()
    print("유니버스 %d종목" % len(syms))
    frames = fetch(syms)
    print("수신 %d종목" % len(frames))
    if len(frames) < len(syms) * 0.8:
        print("수신 실패율 과다 - 중단"); sys.exit(1)
    asof = str(max(df.index[-1] for df in frames.values()).date())
    allrows, cand = scan(frames)
    print("기준일 %s / 후보 %d개" % (asof, len(cand)))

    os.makedirs("log", exist_ok=True)
    cols = ["t", "px", "stop", "m6", "m3", "a20", "a50", "a200", "rsi2", "d5", "frhi", "atrp", "score"]
    if len(cand):
        cand[cols].to_csv("log/cand_%s.csv" % asof, index=False)
    json.dump({"asof": asof, "n_universe": len(frames), "n_cand": int(len(cand)),
               "generated_kst": datetime.now(KST).isoformat(timespec="seconds"),
               "candidates": (cand[cols].to_dict("records") if len(cand) else [])},
              open("s1_result.json", "w"), ensure_ascii=False, indent=1)

    msg = build_message(asof, len(frames), cand)
    print(msg)
    send(msg)


if __name__ == "__main__":
    main()
