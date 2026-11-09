"""用途政策授權產生工具：列出記錄 → 人工逐筆勾選 → 產生 PurposePolicy JSON。

流程（三個子指令）：
  list   用讀取元件（native Query→Result CLI）列出某一資料範圍的全部記錄，
         產生勾選表（Markdown）與對照檔（*.records.json）。預設全部不勾。
  apply  讀勾選表，把打勾的記錄寫成 ai_input=allowed 的授權，輸出用途政策 JSON。
         政策用 modular_answer.PurposePolicy 驗證，舊檔會先備份。
  check  重新列出記錄，對照現有政策：哪些授權仍有效、哪些因內容改變已失效、
         哪些記錄已不存在、哪些記錄沒有授權。有失效授權時結束碼為 1。

授權綁定記錄的身分（scope、source_id、record_id、revision、layer、source_kind，
聊天／逐字稿再加 actor_account_id）與內容雜湊；雜湊和身分都直接使用
modular_answer 的 content_digest／_identity，與回答路徑的檢查一模一樣。
內容只要改變，雜湊就不同，回答路徑會以 approved_content_mismatch 排除該筆。

勾選表與對照檔可能含真實聊天內容，請放在不進 git 的位置。
"""
from __future__ import annotations

import argparse
from datetime import datetime, timezone
import hashlib
import json
from pathlib import Path
import re
import subprocess
import sys

from .modular_answer import ACTOR_DOMAINS, ANSWER_DOMAINS, SCOPE_FIELDS, PurposePolicy, _identity, content_digest, encoded
from .modular_retrieval import PipelineError, load_object

REVIEW_FORMAT = "purpose-grants-review/1"
GRANT_FIELDS = ("scope", "source_id", "record_id", "revision", "layer", "source_kind")
MAX_GRANTS = 200          # PurposePolicy 的上限
PAGE_SIZE = 100           # 讀取元件 Query.limit 的上限
MAX_RECORDS = 5000
PREVIEW_CHARS = 400
GRANT_MARK = re.compile(r"<!-- grant:[0-9a-f]{16} -->")
CHECK_LINE = re.compile(r"^- \[( |x|X)\] 允許送進 AI\s*<!-- grant:([0-9a-f]{16}) -->\s*$")


class GrantToolError(Exception):
    pass


# ---------- 讀取記錄 ----------

def load_json(path: Path) -> dict:
    try:
        return load_object(Path(path).read_bytes())
    except (OSError, PipelineError) as exc:
        raise GrantToolError(f"無法讀取 JSON：{path}（{exc}）") from exc


def read_reader_spec(path: Path) -> dict:
    spec = load_json(path)
    need = {"domain", "command", "cwd", "scope"}
    if not need <= set(spec) or not set(spec) <= need | {"layer", "access_scope"}:
        raise GrantToolError("reader 設定欄位應為 domain、command、cwd、scope（可選 layer、access_scope）")
    if spec["domain"] not in ANSWER_DOMAINS:
        raise GrantToolError(f"domain 必須是 {sorted(ANSWER_DOMAINS)} 之一")
    if (not isinstance(spec["command"], list) or not spec["command"]
            or not all(isinstance(x, str) and x for x in spec["command"])):
        raise GrantToolError("command 必須是非空字串陣列")
    scope = spec["scope"]
    if not isinstance(scope, dict) or set(scope) != set(SCOPE_FIELDS) or scope["domain"] != spec["domain"]:
        raise GrantToolError("scope 必須包含 " + "、".join(SCOPE_FIELDS) + "，且 domain 與上層一致")
    if spec.get("layer", "raw") != "raw":
        raise GrantToolError("回答路徑只接受 raw 層")
    cwd = (Path(path).parent / spec["cwd"]).resolve()
    if not cwd.is_dir():
        raise GrantToolError(f"cwd 不存在：{cwd}")
    return {**spec, "cwd": cwd, "layer": "raw"}


def _selector(scope):
    return {"domain": scope["domain"], **{k: {"mode": "exact", "value": scope[k]} for k in SCOPE_FIELDS[1:]}}


def browse_query(spec: dict, exclude: list[str], page: int) -> dict:
    return {"schema_version": "1.0.1", "request_id": f"purpose-grants-list-{page}", "text": "",
            "intent": "retrieve", "scope": spec["scope"], "actor_account_id": None, "person_id": None,
            "time_from": None, "time_to": None, "exclude_event_ids": exclude, "limit": PAGE_SIZE,
            "layer": "raw", "conditions": {},
            "access_scope": spec.get("access_scope") or {"read_scopes": [_selector(spec["scope"])], "write_scopes": []}}


def run_reader(spec: dict, query: dict) -> dict:
    try:
        proc = subprocess.run(spec["command"], input=json.dumps(query, ensure_ascii=False).encode("utf-8"),
                              cwd=spec["cwd"], capture_output=True, timeout=600)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GrantToolError(f"讀取元件無法執行：{exc}") from exc
    try:
        result = json.loads(proc.stdout.decode("utf-8"))
    except ValueError as exc:   # 含 UnicodeDecodeError
        stderr = proc.stderr.decode("utf-8", "replace")[-800:]
        raise GrantToolError(f"讀取元件輸出不是 UTF-8 JSON（exit={proc.returncode}）：{stderr}") from exc
    status = result.get("status") if isinstance(result, dict) else None
    if proc.returncode != 0 or status not in ("ok", "no_match"):
        raise GrantToolError(f"讀取元件沒有成功：exit={proc.returncode} status={status} "
                             f"reason={result.get('reason_code') if isinstance(result, dict) else None}")
    if result.get("partial"):
        raise GrantToolError("讀取元件回傳 partial，不能當成完整清單")
    evidence = result.get("evidence") or []
    if not isinstance(evidence, list):
        raise GrantToolError("讀取元件的 evidence 不是陣列")
    return result


def fetch_records(spec: dict) -> list[dict]:
    """用空白查詢（瀏覽全部）分頁取回；以 source_event_id 排除已取得的記錄。"""
    records, seen, page = [], [], 0
    while True:
        page += 1
        evidence = run_reader(spec, browse_query(spec, seen, page)).get("evidence") or []
        fresh = [e for e in evidence if e.get("source_event_id") not in seen]
        if len(fresh) != len(evidence):
            raise GrantToolError("讀取元件回傳了已排除的記錄，無法安全分頁")
        records += evidence
        seen += [e["source_event_id"] for e in evidence]
        if len(evidence) < PAGE_SIZE:
            break
        if len(records) > MAX_RECORDS:
            raise GrantToolError(f"記錄超過 {MAX_RECORDS} 筆，請縮小 scope")
    return records


def read_evidence_file(path: Path) -> list[dict]:
    """離線輸入：每行一筆 evidence 的 JSONL（測試或沒有讀取元件時用）。"""
    out = []
    for n, line in enumerate(Path(path).read_text(encoding="utf-8-sig").splitlines(), 1):
        if line.strip():
            try:
                out.append(json.loads(line))
            except ValueError as exc:
                raise GrantToolError(f"{path} 第 {n} 行不是 JSON") from exc
    return out


def load_records(args) -> list[dict]:
    if bool(args.reader) == bool(args.evidence):
        raise GrantToolError("--reader 與 --evidence 擇一")
    records = fetch_records(read_reader_spec(Path(args.reader))) if args.reader else read_evidence_file(Path(args.evidence))
    return [describe(r) for r in records]


def describe(evidence: dict) -> dict:
    """抽出授權需要的身分與內容雜湊；用回答路徑相同的函式計算。"""
    try:
        domain = evidence["scope"]["domain"]
        if domain not in ANSWER_DOMAINS:
            raise GrantToolError(f"記錄 {evidence.get('record_id')} 的 domain 不支援：{domain}")
        identity = {k: evidence[k] for k in GRANT_FIELDS}
        if domain in ACTOR_DOMAINS:
            identity["actor_account_id"] = evidence["actor_account_id"]
        key_bytes = _identity(evidence)
        digest = content_digest(evidence)
    except (KeyError, TypeError) as exc:
        raise GrantToolError(f"記錄缺少必要欄位：{exc}（record_id={evidence.get('record_id')}）") from exc
    return {"key": hashlib.sha256(key_bytes).hexdigest()[:16], "identity_bytes": key_bytes, "identity": identity,
            "content_sha256": digest, "event_time": evidence.get("event_time"), "text": evidence.get("text") or "",
            "quality_flags": evidence.get("quality_flags") or []}


# ---------- 對照現有政策 ----------

def policy_grants(path: Path | None) -> dict[bytes, dict]:
    if path is None or not Path(path).is_file():
        return {}
    value = load_json(path)
    try:
        PurposePolicy(value)
    except PipelineError as exc:
        raise GrantToolError(f"現有政策格式不正確：{exc}") from exc
    return {_identity(g): g for g in value["grants"]}


def classify(records: list[dict], grants: dict[bytes, dict]) -> tuple[list[dict], list[dict]]:
    """每筆記錄的目前狀態，以及政策裡已找不到記錄的授權。"""
    out = []
    for r in records:
        g = grants.get(r["identity_bytes"])
        if g is None:
            state = "none"
        elif g["ai_input"]["status"] != "allowed":
            state = g["ai_input"]["status"]
        elif g["content_sha256"] == r["content_sha256"]:
            state = "allowed"
        else:
            state = "stale"
        out.append({**r, "state": state})
    present = {r["identity_bytes"] for r in records}
    orphans = [g for k, g in grants.items() if k not in present]
    return out, orphans


STATE_TEXT = {"none": "目前未授權", "allowed": "目前已授權（內容未變）",
              "stale": "⚠ 原本已授權，但內容已改變，授權已失效；要繼續允許請重新勾選",
              "prohibited": "目前標為禁止", "unknown": "目前標為未知"}


# ---------- list ----------

def _quote(text: str) -> str:
    short = text if len(text) <= PREVIEW_CHARS else text[:PREVIEW_CHARS] + "……（以下省略）"
    return "\n".join("> " + line if line else ">" for line in short.splitlines() or [""])


def write_review(path: Path, policy_id: str, rows: list[dict], orphans: list[dict]) -> Path:
    if len({r["key"] for r in rows}) != len(rows):
        raise GrantToolError("有重複的記錄身分，無法產生勾選表")
    now = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    review_id = hashlib.sha256(encoded([r["key"] + r["content_sha256"] for r in rows]) + now.encode()).hexdigest()[:16]
    lines = [f"# 用途授權勾選表：{policy_id}", "",
             f"產生時間：{now}　｜　共 {len(rows)} 筆　｜　review_id：{review_id}", "",
             "要允許送進 AI 的記錄，把「允許送進 AI」前面方括號裡的空白改成 x；不勾＝不允許。",
             "每筆勾選行後面的 grant 編號不要改，也不要刪掉任何一筆。改完執行 apply。",
             "授權綁定這一版的內容；內容之後若改變，授權會自動失效。", ""]
    for i, r in enumerate(rows, 1):
        ident = r["identity"]
        actor = f"　｜　發言者：{ident['actor_account_id']}" if "actor_account_id" in ident else ""
        lines += [f"## {i}. {ident['record_id']}（第 {ident['revision']} 版）", "",
                  f"- [{'x' if r['state'] == 'allowed' else ' '}] 允許送進 AI <!-- grant:{r['key']} -->",
                  f"- 狀態：{STATE_TEXT.get(r['state'], r['state'])}",
                  f"- 時間：{r['event_time']}　｜　來源：{ident['source_id']}{actor}",
                  f"- 標記：{'、'.join(r['quality_flags']) or '（無）'}", "", _quote(r["text"]), ""]
    if orphans:
        lines += ["## 政策裡有、但記錄已不存在的授權（apply 後會移除）", ""]
        lines += [f"- {g['record_id']}（第 {g['revision']} 版，來源 {g['source_id']}，{g['ai_input']['status']}）" for g in orphans]
        lines.append("")
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    sidecar = sidecar_path(path)
    sidecar.write_text(json.dumps({"format": REVIEW_FORMAT, "review_id": review_id, "policy_id": policy_id,
                                   "generated_at": now,
                                   "records": [{"key": r["key"], "identity": r["identity"],
                                                "content_sha256": r["content_sha256"]} for r in rows]},
                                  ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    path.write_text("\n".join(lines), encoding="utf-8")
    return sidecar


def sidecar_path(review: Path) -> Path:
    return Path(review).with_name(Path(review).stem + ".records.json")


# ---------- apply ----------

def read_checks(review: Path) -> tuple[str, dict[str, bool]]:
    text = Path(review).read_text(encoding="utf-8-sig")
    m = re.search(r"review_id：([0-9a-f]{16})", text)
    if not m:
        raise GrantToolError("勾選表找不到 review_id")
    checks = {}
    for line in text.splitlines():
        if GRANT_MARK.search(line):
            hit = CHECK_LINE.match(line.strip())
            if not hit:
                raise GrantToolError(f"看不懂這一行（請只改方括號內的空白或 x）：{line.strip()}")
            if hit.group(2) in checks:
                raise GrantToolError(f"同一筆記錄出現兩次：{hit.group(2)}")
            checks[hit.group(2)] = hit.group(1) in "xX"
    return m.group(1), checks


def build_policy(review: Path, policy_id: str | None, basis: str) -> tuple[dict, list[dict]]:
    side = load_json(sidecar_path(review))
    if side.get("format") != REVIEW_FORMAT:
        raise GrantToolError("對照檔格式不符")
    review_id, checks = read_checks(review)
    if review_id != side["review_id"]:
        raise GrantToolError("勾選表與對照檔不是同一次 list 產生的")
    keys = [r["key"] for r in side["records"]]
    if set(checks) != set(keys):
        missing, extra = set(keys) - set(checks), set(checks) - set(keys)
        raise GrantToolError(f"勾選表的記錄和對照檔不一致：缺少 {sorted(missing)}，多出 {sorted(extra)}")
    chosen = [r for r in side["records"] if checks[r["key"]]]
    if len(chosen) > MAX_GRANTS:
        raise GrantToolError(f"一份政策最多 {MAX_GRANTS} 筆授權，目前勾了 {len(chosen)} 筆")
    grants = [{**r["identity"], "content_sha256": r["content_sha256"],
               "ai_input": {"status": "allowed", "basis": basis}} for r in chosen]
    policy = {"schema_version": "1.0.1", "policy_id": policy_id or side["policy_id"], "grants": grants}
    try:
        PurposePolicy(policy)
    except PipelineError as exc:
        raise GrantToolError(f"產生的政策沒有通過系統驗證：{exc}") from exc
    return policy, chosen


def write_policy(path: Path, policy: dict) -> Path | None:
    path = Path(path)
    backup = None
    if path.exists():
        stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
        backup = path.with_name(f"{path.name}.bak-{stamp}")
        backup.write_bytes(path.read_bytes())
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    tmp.write_text(json.dumps(policy, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    tmp.replace(path)
    return backup


# ---------- CLI ----------

def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="python -m ssr_chat.purpose_grants", description="用途政策授權產生工具")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("list", "check"):
        p = sub.add_parser(name)
        src = p.add_mutually_exclusive_group(required=True)
        src.add_argument("--reader", help="讀取元件設定 JSON（domain、command、cwd、scope）")
        src.add_argument("--evidence", help="離線 evidence JSONL")
        p.add_argument("--policy", help="現有用途政策 JSON（可不存在）", required=(name == "check"))
        if name == "list":
            p.add_argument("--out", required=True, help="勾選表 .md 路徑（另產生同名 .records.json）")
            p.add_argument("--policy-id", help="新政策 id；預設沿用現有政策或依 scope 產生")
    p = sub.add_parser("apply")
    p.add_argument("--review", required=True)
    p.add_argument("--policy", required=True, help="輸出的用途政策 JSON；已存在會先備份")
    p.add_argument("--policy-id")
    p.add_argument("--basis", help="授權依據（寫入每筆 grant）")
    args = ap.parse_args(argv)
    try:
        if args.cmd == "apply":
            basis = args.basis or f"operator checked in purpose_grants review on {datetime.now(timezone.utc):%Y-%m-%d}"
            policy, chosen = build_policy(Path(args.review), args.policy_id, basis)
            backup = write_policy(Path(args.policy), policy)
            print(f"已寫入政策：{args.policy}（允許 {len(chosen)} 筆）")
            if backup:
                print(f"舊政策備份：{backup}")
            return 0
        records = load_records(args)
        rows, orphans = classify(records, policy_grants(Path(args.policy) if args.policy else None))
        if args.cmd == "list":
            policy_id = args.policy_id
            if not policy_id and args.policy and Path(args.policy).is_file():
                policy_id = load_json(Path(args.policy))["policy_id"]
            if not policy_id:
                scope = records[0]["identity"]["scope"] if records else {}
                policy_id = "-".join(str(scope.get(k)) for k in ("domain", "platform", "channel_id") if scope.get(k)) or "policy"
            sidecar = write_review(Path(args.out), policy_id, rows, orphans)
            print(f"勾選表：{args.out}（{len(rows)} 筆）\n對照檔：{sidecar}")
            return 0
        counts = {s: sum(1 for r in rows if r["state"] == s) for s in ("allowed", "stale", "none", "prohibited", "unknown")}
        print(f"記錄 {len(rows)} 筆：有效授權 {counts['allowed']}、內容已變失效 {counts['stale']}、"
              f"未授權 {counts['none']}、禁止 {counts['prohibited']}、未知 {counts['unknown']}；"
              f"記錄已不存在的授權 {len(orphans)}")
        for r in rows:
            if r["state"] == "stale":
                print(f"  失效：{r['identity']['record_id']}（第 {r['identity']['revision']} 版）")
        for g in orphans:
            print(f"  記錄不存在：{g['record_id']}（第 {g['revision']} 版）")
        return 1 if counts["stale"] or orphans else 0
    except GrantToolError as exc:
        print(f"錯誤：{exc}", file=sys.stderr)
        return 2


if __name__ == "__main__":
    raise SystemExit(main())
