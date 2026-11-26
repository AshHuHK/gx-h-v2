"""Bind a scoped PI decision, the original outstanding exposure and a single successor cohort."""
import argparse
from hashlib import sha256
import json
import os
from pathlib import Path
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[2]
sys.path[:0] = [str(ROOT), str(ROOT / "gx-h")]
from acceptance.v622.public.enrollment import digest, read
from acceptance.v623.oauth import authorization as original
from acceptance.v623.oauth.judge import save as save_once
from acceptance.v623.oauth.usage import reconcile

DIAGNOSTIC = "GX-V62-CLOSE-ACTION-OAUTH-01"
SCIENCE = "GX-V624-INDEPENDENT-DEPOSITION-01"
QUESTION = ("本機現有批准只涵蓋舊四格。是否授權同一個現有 ChatGPT OAuth 帳戶，在保留舊格至少 578,596 token、尾段仍未知的紀錄下，另開新系列？"
            "候選提交在首筆推理前固定，只走官方宿主、追加金額 0、不切 API key、不設 token 上限。診斷只跑 P4 單層與 K7 指定委派各一次，每案 15 分鐘開工、6 分鐘收尾、2 分鐘對帳；"
            "任一新案用量不清即停。若兩案均驗收且材料合格，另開一次第三沉積研究（30＋6＋2 分鐘），獨立數值複算用 CPU。")
CHOICES = ("授權兩案及條件式第三沉積研究，接受舊未結曝險並保留原帳",
           "只授權兩案診斷，接受舊未結曝險並保留原帳",
           "只做工程、離線演練與材料資格篩選")
TERMS = {"provider":"openai","authentication":"chatgpt","additional_currency_authorized":0,
         "api_key_fallback":False,"purchase_credits":False,"token_cap":None,
         "diagnostic_seconds":[900,360,120],"science_seconds":[1800,360,120],
         "stop_on_new_unresolved":True,"replacement":False}


def user_choice(conversation):
    result = None
    original_requests = set()
    with original._source(conversation).open(encoding="utf-8") as rows:
        for line in rows:
            if "request_user_input_async" not in line:
                continue
            event = json.loads(line)
            msg = event.get("payload", {})
            if event.get("type") != "response_item":
                continue
            if msg.get("type") == "function_call" and msg.get("name") == "request_user_input_async":
                questions = json.loads(msg["arguments"]).get("questions", [])
                for index, question in enumerate(questions):
                    if question.get("title") == QUESTION and set(question.get("options", ())) == set(CHOICES):
                        original_requests.add((msg["call_id"], index))
                continue
            if (msg.get("type"),msg.get("role")) != ("message","user"):
                continue
            for item in msg.get("content", []):
                body = item.get("text", "")
                matched = re.fullmatch(r"\s*<send_user_message_question_reply>\s*(.*?)\s*</send_user_message_question_reply>\s*", body, re.S)
                if not matched:
                    continue
                for answer in json.loads(matched[1]):
                    ident = json.loads(answer["questionItemId"])
                    if len(ident) != 3 or ident[0] != "request_user_input_async":
                        continue
                    shown, choice = answer.get("question", ""), answer.get("answer", "")
                    # The client echoes up to 512 UTF-8 bytes. Recover the full terms
                    # only from the original request bound to this user's reply.
                    matched_call = (ident[1], ident[2]) in original_requests
                    client_echo = QUESTION.encode("utf-8")[:512].decode("utf-8", errors="ignore")
                    paired = matched_call and shown in (QUESTION, client_echo)
                    if paired and re.fullmatch(r"(?:授权所有|授權所有)[，,]\s*不[设設]\s*token\s*(?:上限|上线|上線)", choice, re.I):
                        choice = CHOICES[0]
                    if (shown == QUESTION or paired) and choice in CHOICES:
                        result = {"choice":choice,"utc":event["timestamp"],
                                  "message_sha256":sha256(body.encode()).hexdigest()}
                    elif paired:
                        result = None
    if result is None or result["choice"] == CHOICES[2]:
        raise ValueError("No affirmative PI approval covering the new diagnostic and original exposure")
    return result


def predecessor(path, account_ref):
    work = Path(path).resolve(strict=True)
    reg = read(work/"registration.json")
    public = ROOT/"acceptance/v623/oauth/receipts"
    first = read(public/"first-attempts/index.json")
    frozen = read(public/"interrupted-turn.json")
    if (digest(work/"registration.json") != first["registration_sha256"]
            or reg["series"] != original.SERIES or account_ref != reg["account_ref"]):
        raise ValueError("Original enrollment or subscription account differs")
    rid = frozen["run_id"]
    if read(work/"01/first-attempt.json")["run_id"] != rid:
        raise ValueError("Original held Run differs")
    current = reconcile(work/"01",rid)
    held = {(t["thread"],t["turn"]) for t in current["pending_turns"]+current["unresolved"]
            if "thread" in t and "turn" in t}
    if current["usage_status"] != "SETTLED" and (
            held != {(frozen["thread_id"],frozen["turn_id"])}
            or any("thread" not in t or "turn" not in t for t in current["unresolved"])):
        raise ValueError("The unresolved exposure differs from the original authorized tail")
    for slot in ("02","03","04"):
        if (work/slot/"first-attempt.json").exists():
            raise ValueError("The predecessor gained an additional original attempt; recheck the exposure")
    return {"registration_sha256":first["registration_sha256"],"run_id":rid,
            "lower_bound_at_approval":frozen["acknowledged_response_lower_bound"],
            "original_tail_turn":frozen["turn_id"],"account_ref":account_ref}


def enrollment(conversation):
    # Use the same private operator area as the original approval, independent of the checkout.
    return original._enrollment(conversation).with_name(conversation+"-"+DIAGNOSTIC+".json")


def find_enrollment(original_digest):
    directory = Path.home() / ".local/state/gx-h/approved-pilots"
    matches = [p for p in directory.glob("*-"+DIAGNOSTIC+".json") if digest(p)==original_digest]
    if len(matches)!=1:
        raise ValueError("The original scoped PI approval is unavailable")
    return matches[0]


def issue(conversation, *, diagnostic_work, science_work, old_work):
    evidence = user_choice(conversation)
    candidate = subprocess.check_output(["git","rev-parse","HEAD"],cwd=ROOT,text=True).strip()
    if subprocess.check_output(["git","status","--porcelain"],cwd=ROOT):
        raise ValueError("Register the tested clean candidate before first inference")
    paths = {DIAGNOSTIC:str(Path(diagnostic_work).resolve())}
    if evidence["choice"] == CHOICES[0]:
        paths[SCIENCE] = str(Path(science_work).resolve())
    if len(set(paths.values())) != len(paths) or any(Path(p).is_relative_to(ROOT) for p in paths.values()):
        raise ValueError("Each series requires its own external work directory")
    source = Path(old_work).resolve()
    account = read(source/"registration.json")["account_ref"]
    prior = predecessor(source,account)
    doc = {"schema":"gx.science-execution.pi/1","conversation":conversation,"pi":evidence,
           "candidate":candidate,"work":paths,"account_ref":account,"terms":TERMS,
           "original_work":str(source),"retained_exposure":prior}
    path = enrollment(conversation)
    save_once(path,doc)
    return path


def verify(approval_path, *, work, candidate, series, account_ref):
    if not approval_path:
        raise ValueError("PI approval for this successor series is required")
    path = Path(approval_path).resolve(strict=True)
    doc = read(path)
    if doc.get("schema") != "gx.science-execution.pi/1":
        raise ValueError("A four-cell or unrelated approval does not cover the successor")
    if (path != enrollment(doc["conversation"]) or doc["pi"] != user_choice(doc["conversation"])
            or doc["candidate"] != candidate or not re.fullmatch("[a-f0-9]{40}",candidate)
            or doc["terms"] != TERMS or doc["account_ref"] != account_ref
            or doc["work"].get(series) != str(Path(work).resolve())
            or series not in (DIAGNOSTIC,SCIENCE)
            or (series == SCIENCE and doc["pi"]["choice"] != CHOICES[0])
            or doc["retained_exposure"] != predecessor(doc["original_work"],account_ref)):
        raise ValueError("The PI approval differs in account, scope, candidate or original exposure")
    return {**doc,"approval_sha256":digest(path)}


def verify_diagnostics(work, *, candidate, account_ref,approval_path=None):
    from acceptance.science_execution.witness import freeze_ready
    return freeze_ready(work,candidate=candidate,account_ref=account_ref,approval_path=approval_path)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    for opt in ("diagnostic-work","science-work","old-work"):
        parser.add_argument("--"+opt,type=Path,required=True)
    args = parser.parse_args()
    print(issue(os.environ["CODEX_THREAD_ID"],diagnostic_work=args.diagnostic_work,
                science_work=args.science_work,old_work=args.old_work))
