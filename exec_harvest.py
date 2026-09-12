"""
실행(Execution) 계측 수확기 — exec_harvest.py   [schema v1]

■ 무엇을 하는가
  깃 히스토리에 남아 있는 scanner2_result.json / positions.json 스냅샷을 전부 꺼내
  덧붙이기 전용(append-only) 로그 exec_log/YYYY-MM.jsonl.gz 로 재구성한다.

■ 왜 필요한가
  두 파일은 매 실행마다 통째로 덮어쓰이므로 최신 상태만 남는다. 다만 워크플로우가
  매번 커밋하기 때문에 깃 히스토리에는 전 시점이 보존돼 있다. 분석할 때마다 수백 개
  커밋을 다시 걸어가는 건 느리고, 무엇보다 "어떤 필드를 남길지"를 미리 확정해두지
  않으면 나중에 하고 싶은 분석을 못 하게 된다(판호2: 계측 스키마 설계가 앞으로
  가능한 분석의 범위를 결정한다).

■ 안전 규칙 (이 파일의 존재 이유이기도 함)
  - 기존 파일을 절대 수정하지 않는다. git show 로 읽고, 새 파일만 쓴다.
  - 멱등(idempotent): 처리한 커밋 sha 를 exec_log/_state.json 에 남기고 건너뛴다.
    몇 번을 다시 돌려도 같은 레코드가 두 번 쌓이지 않는다.
  - 깃허브 API를 쓰지 않는다(호출 한도 없음). 로컬 git 명령만 쓴다.
    → 워크플로우에서 반드시 fetch-depth: 0 으로 전체 히스토리를 받아와야 한다.

■ 기록 형식 (한 줄에 JSON 하나, gzip)
  후보:   {"t":"cand", day, ts, sym, px, sig, gate, g1..g5, tA,tB,tC, tcnt,
           vol, vwap, orb, sha}
  포지션: {"t":"pos", sym, entry_px, entry_ts, entry_day, bcl, peak, status,
           exit_px, exit_ts, reasons, pnl, urgent, sha, ts}
  메타:   {"t":"scan", ts, sha, n_cand, n_checked, n_entry, n_err, cooldown}

  포지션은 "상태가 바뀔 때마다" 기록한다(peak_price 궤적이 남아야
  '안 팔았으면 어땠나'를 재구성할 수 있다). 값이 그대로면 기록하지 않는다.
"""

import gzip
import hashlib
import json
import os
import subprocess
from collections import defaultdict

LOG_DIR = "exec_log"
STATE_PATH = os.path.join(LOG_DIR, "_state.json")
SCHEMA_VERSION = 1

CAND_FILE = "scanner2_result.json"
POS_FILE = "positions.json"


# ─────────────────────────── git 헬퍼 ───────────────────────────

def git(*args):
    r = subprocess.run(("git",) + args, capture_output=True, text=True)
    if r.returncode != 0:
        raise RuntimeError(f"git {' '.join(args)} 실패: {r.stderr.strip()[:300]}")
    return r.stdout


def commits_for(path):
    """해당 파일을 건드린 커밋 전부를 (sha, 커밋시각ISO) 오래된 순으로."""
    try:
        out = git("log", "--reverse", "--format=%H\t%cI", "--", path)
    except RuntimeError:
        return []
    rows = []
    for line in out.splitlines():
        line = line.strip()
        if not line or "\t" not in line:
            continue
        sha, iso = line.split("\t", 1)
        rows.append((sha, iso))
    return rows


def blob_json(sha, path):
    """특정 커밋 시점의 파일 내용을 JSON으로. 없거나 깨졌으면 None."""
    try:
        raw = git("show", f"{sha}:{path}")
    except RuntimeError:
        return None
    try:
        return json.loads(raw)
    except Exception:
        return None


# ─────────────────────────── 레코드 생성 ───────────────────────────

def _f(x):
    """숫자만 통과, 나머지는 None. (NaN 문자열/불리언 섞임 방지)"""
    if isinstance(x, bool) or x is None:
        return None
    try:
        v = float(x)
    except (TypeError, ValueError):
        return None
    return v if v == v else None   # NaN 제거


def cand_records(sha, commit_iso, doc):
    """scanner2_result.json 한 스냅샷 → 스캔 메타 1건 + 후보 N건."""
    ts = doc.get("updated_at_utc") or commit_iso
    day = str(ts)[:10]
    rows = doc.get("all_results") or []

    yield {
        "t": "scan", "ts": ts, "day": day, "sha": sha,
        "n_cand": doc.get("candidate_count"),
        "n_checked": doc.get("checked_count"),
        "n_entry": len(doc.get("entries") or []),
        "n_err": doc.get("error_count"),
        "cooldown": doc.get("cooldown_excluded") or [],
    }

    for r in rows:
        if not isinstance(r, dict) or not r.get("symbol"):
            continue
        if r.get("error"):
            # 에러 항목도 남긴다 — "그날 데이터를 못 받아서 못 산 것"과
            # "조건 미달로 안 산 것"은 완전히 다른 사건이다.
            yield {"t": "cand", "day": day, "ts": ts, "sha": sha,
                   "sym": r["symbol"], "err": str(r["error"])[:120]}
            continue
        g = r.get("hard_gate") or {}
        a = r.get("action_trigger") or {}
        yield {
            "t": "cand", "day": day, "ts": ts, "sha": sha,
            "sym": r["symbol"],
            "px": _f(r.get("price")),
            "sig": bool(r.get("entry_signal")),
            "gate": bool(r.get("hard_gate_passed")),
            "g1": g.get("1_ma20_above_ma50"),
            "g2": g.get("2_ma50_slope_up"),
            "g3": g.get("3_vwap_break"),
            "g4": g.get("4_volume_confirmed"),
            "g5": g.get("5_ma200_trend_or_skip"),
            "ma200_avail": r.get("ma200_available"),
            "tA": a.get("A_prev_day_high_break"),
            "tB": a.get("B_premarket_high_break"),
            "tC": a.get("C_orb_high_break"),
            "tcnt": r.get("action_trigger_count"),
            "vol": r.get("volume_confirmed"),
            "vwap": _f(r.get("vwap")),
            "orb": _f(r.get("orb_high")),
        }


POS_FIELDS = ("entry_price", "entry_time_utc", "entry_date_ny", "breakout_candle_low",
              "peak_price", "status", "exit_price", "exit_time_utc", "exit_reasons",
              "pnl_pct", "urgent", "partial_profit_alerted")


def pos_state_key(p):
    """포지션 하나를 식별하는 키 — 심볼 + 진입시각."""
    return f"{p.get('symbol')}|{p.get('entry_time_utc')}"


def pos_fingerprint(p):
    """상태가 바뀌었는지 판정할 지문."""
    payload = json.dumps({k: p.get(k) for k in POS_FIELDS}, sort_keys=True, default=str)
    return hashlib.sha256(payload.encode()).hexdigest()[:16]


def pos_records(sha, commit_iso, doc, seen_fp):
    """positions.json 한 스냅샷 → 값이 바뀐 포지션만.

    중복 감지: 같은 (심볼, 진입시각)이 한 스냅샷 안에 두 번 이상 나오면 별도 레코드로
    남긴다. 2026-09-03 에 실제로 발생했던 문제(같은 종목이 같은 진입시각으로 최대 4번
    기록되어 성과 통계가 부풀려짐)를 조용히 정규화해버리지 않기 위한 장치다.
    """
    positions = doc.get("positions") if isinstance(doc, dict) else doc
    if not isinstance(positions, list):
        return

    counts = defaultdict(int)
    for p in positions:
        if isinstance(p, dict) and p.get("symbol"):
            counts[pos_state_key(p)] += 1
    dups = {k: v for k, v in counts.items() if v > 1}
    if dups:
        yield {"t": "posdup", "ts": commit_iso, "sha": sha,
               "n_rows": len(positions), "n_unique": len(counts), "dups": dups}

    for p in positions:
        if not isinstance(p, dict) or not p.get("symbol"):
            continue
        key = pos_state_key(p)
        fp = pos_fingerprint(p)
        if seen_fp.get(key) == fp:
            continue                      # 그대로면 기록하지 않는다
        seen_fp[key] = fp
        yield {
            "t": "pos", "ts": commit_iso, "sha": sha,
            "sym": p.get("symbol"),
            "entry_px": _f(p.get("entry_price")),
            "entry_ts": p.get("entry_time_utc"),
            "entry_day": p.get("entry_date_ny"),
            "bcl": _f(p.get("breakout_candle_low")),
            "peak": _f(p.get("peak_price")),
            "status": p.get("status"),
            "exit_px": _f(p.get("exit_price")),
            "exit_ts": p.get("exit_time_utc"),
            "reasons": p.get("exit_reasons") or [],
            "pnl": _f(p.get("pnl_pct")),
            "urgent": p.get("urgent"),
            "partial": p.get("partial_profit_alerted"),
        }


# ─────────────────────────── 저장 ───────────────────────────

def load_state():
    if not os.path.exists(STATE_PATH):
        return {"schema": SCHEMA_VERSION, "done_cand": [], "done_pos": [], "pos_fp": {}}
    with open(STATE_PATH) as f:
        s = json.load(f)
    s.setdefault("done_cand", [])
    s.setdefault("done_pos", [])
    s.setdefault("pos_fp", {})
    s["schema"] = SCHEMA_VERSION
    return s


def save_state(s):
    os.makedirs(LOG_DIR, exist_ok=True)
    with open(STATE_PATH, "w") as f:
        json.dump(s, f, indent=1, ensure_ascii=False, sort_keys=True)


def write_records(records):
    """월별 gzip jsonl 에 덧붙인다. gzip은 멤버를 이어붙여도 정상적으로 읽힌다."""
    by_month = defaultdict(list)
    for r in records:
        stamp = r.get("ts") or r.get("entry_ts") or ""
        month = str(stamp)[:7] or "unknown"
        by_month[month].append(r)

    os.makedirs(LOG_DIR, exist_ok=True)
    written = {}
    for month, rows in sorted(by_month.items()):
        path = os.path.join(LOG_DIR, f"{month}.jsonl.gz")
        with gzip.open(path, "at", encoding="utf-8") as f:
            for r in rows:
                f.write(json.dumps(r, ensure_ascii=False, sort_keys=True) + "\n")
        written[path] = len(rows)
    return written


# ─────────────────────────── 메인 ───────────────────────────

def main():
    state = load_state()
    done_cand = set(state["done_cand"])
    done_pos = set(state["done_pos"])
    pos_fp = dict(state["pos_fp"])

    records = []

    cand_commits = commits_for(CAND_FILE)
    new_cand = [(s, i) for s, i in cand_commits if s not in done_cand]
    print(f"[후보] 커밋 {len(cand_commits)}개 중 신규 {len(new_cand)}개")
    for sha, iso in new_cand:
        doc = blob_json(sha, CAND_FILE)
        if doc is None:
            print(f"  건너뜀(파싱실패) {sha[:8]}")
        else:
            records.extend(cand_records(sha, iso, doc))
        done_cand.add(sha)

    pos_commits = commits_for(POS_FILE)
    new_pos = [(s, i) for s, i in pos_commits if s not in done_pos]
    print(f"[포지션] 커밋 {len(pos_commits)}개 중 신규 {len(new_pos)}개")
    # 포지션은 '변화분만' 기록하므로 반드시 시간순으로 처리해야 한다.
    for sha, iso in new_pos:
        doc = blob_json(sha, POS_FILE)
        if doc is None:
            print(f"  건너뜀(파싱실패) {sha[:8]}")
        else:
            records.extend(pos_records(sha, iso, doc, pos_fp))
        done_pos.add(sha)

    if not records:
        print("새로 쌓을 레코드 없음.")
        state["done_cand"] = sorted(done_cand)
        state["done_pos"] = sorted(done_pos)
        state["pos_fp"] = pos_fp
        save_state(state)
        return

    written = write_records(records)
    state["done_cand"] = sorted(done_cand)
    state["done_pos"] = sorted(done_pos)
    state["pos_fp"] = pos_fp
    save_state(state)

    kinds = defaultdict(int)
    for r in records:
        kinds[r["t"]] += 1
    print(f"\n총 {len(records)}건 기록: " + ", ".join(f"{k}={v}" for k, v in sorted(kinds.items())))
    for path, n in written.items():
        size = os.path.getsize(path)
        print(f"  {path}  +{n}건  (파일 {size:,} 바이트)")


if __name__ == "__main__":
    main()
