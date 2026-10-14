"""以離線結果組成提交資料；明確要求實送設定。"""

import argparse
import json
import math
import os
import re
import subprocess
import sys
from pathlib import Path
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

from dotenv import load_dotenv

from scenarios.runner import SAMPLE_QUESTIONS, load_questions
from gateway.token_store import read_token


class _NoRedirect(HTTPRedirectHandler):
    """不跟隨重新導向，避免 Authorization 被轉送到其他主機。"""

    def redirect_request(self, *args, **kwargs):
        return None


def require_https(url: str) -> str:
    """F16：正式連線一律 https；只有 localhost 與 127.0.0.1 可用 http（本機測試）。"""
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.hostname:
        return url
    if parsed.scheme == "http" and parsed.hostname in {"localhost", "127.0.0.1"}:
        return url
    raise ValueError("連線網址必須是 https（本機 localhost／127.0.0.1 除外）")


def urlopen(request: Request, timeout: float):
    return build_opener(_NoRedirect).open(request, timeout=timeout)


def remote_questions(token: str) -> list[dict]:
    gateway = os.getenv("GATEWAY_URL", "").strip()
    if not gateway:
        raise ValueError("缺少 GATEWAY_URL")
    request = Request(require_https(gateway.rstrip("/") + "/questions"), headers={"Authorization": f"Bearer {token}"})
    with urlopen(request, timeout=15) as response:
        payload = json.load(response)
    questions = payload.get("questions") if isinstance(payload, dict) else payload
    if not isinstance(questions, list) or len(questions) != 10:
        raise ValueError("遠端題組必須剛好有 10 題")
    try:
        ids = [question["id"] for question in questions]
        if len(set(ids)) != 10 or any(
            not isinstance(question["expected_keywords"], list)
            or not question["expected_keywords"]
            or not all(isinstance(word, str) and word for word in question["expected_keywords"])
            for question in questions
        ):
            raise ValueError("題組格式錯誤")
    except (KeyError, TypeError) as exc:
        raise ValueError("題組格式錯誤") from exc
    return questions


def score_check(level: str, file: Path | None) -> dict:
    if file is None:
        environment = {**os.environ, "PROVIDER": "mock"}
        result = subprocess.run(
            [sys.executable, "-m", f"checks.{level}"],
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            env=environment,
            check=False,
        )
        if result.returncode:
            raise RuntimeError(f"{level} 檢查執行失敗：{result.stderr.strip() or result.stdout.strip()}")
        text = result.stdout
    else:
        text = file.read_text(encoding="utf-8")
    total = 6 if level == "l1" else 3
    matches = re.findall(rf"(?<!\d)(\d+)/{total}(?!\d)", text)
    if not matches:
        raise ValueError(f"{level} 結果缺少 x/{total} 分數")
    score = int(matches[-1])
    if score > total:
        raise ValueError(f"{level} 分數超過 {total}")
    return {"passed": score, "total": total}


def build_payload(
    questions_path: Path,
    answers_path: Path,
    usage_path: Path,
    l1_path: Path | None = None,
    l2_path: Path | None = None,
    questions: list[dict] | None = None,
) -> dict:
    if questions is None:
        questions = load_questions(questions_path)
    recorded = json.loads(answers_path.read_text(encoding="utf-8"))
    answers = recorded["answers"]
    if len(answers) != 10 or [a["id"] for a in answers] != [q["id"] for q in questions]:
        raise ValueError("答案必須與題組中的 10 題依序對應")
    with usage_path.open(encoding="utf-8") as handle:
        usage = [json.loads(line) for line in handle if line.strip()]
    start, end = recorded["usage_start"], recorded["usage_end"]
    if not isinstance(start, int) or not isinstance(end, int) or not 0 <= start <= end <= len(usage):
        raise ValueError("答案記錄中的 usage 範圍無效")
    relevant = usage[start:end]
    if not relevant:
        raise ValueError("找不到這 10 題的 usage 紀錄")
    successful = [row for row in relevant if row.get("status") == "ok"]
    if len(successful) != 10 or any(row.get("provider") != "mock" for row in successful):
        raise ValueError("10 題必須各有一筆成功的 mock usage 紀錄")
    costs = [float(row["cost_usd"]) for row in relevant]
    if any(not math.isfinite(cost) or cost < 0 for cost in costs):
        raise ValueError("usage 成本必須是非負有限數值")
    quality = [
        {
            "id": question["id"],
            "passed": all(
                word.casefold() in answer["answer"].casefold()
                for word in question["expected_keywords"]
            ),
        }
        for question, answer in zip(questions, answers)
    ]
    return {
        "nickname": os.getenv("NICKNAME", ""),
        "l1": score_check("l1", l1_path),
        "l2": score_check("l2", l2_path),
        "questions": quality,
        "quality_passed": sum(item["passed"] for item in quality),
        "question_count": 10,
        "total_tokens": sum(row.get("input_tokens", 0) + row.get("output_tokens", 0) for row in relevant),
        "total_cost_usd": round(sum(costs), 8),
        "pricing": "mock 教學用固定價格，非官方定價",
    }


def main() -> None:
    load_dotenv(".env")
    parser = argparse.ArgumentParser(description="組成成果 JSON 或提交至 Worker")
    parser.add_argument("--dry-run", action="store_true", help="只印出 JSON，不連線")
    parser.add_argument("--questions", type=Path, default=SAMPLE_QUESTIONS)
    parser.add_argument("--answers", type=Path, default=Path("answers.json"))
    parser.add_argument("--usage", type=Path, default=Path(os.getenv("USAGE_LOG", "usage.jsonl")))
    parser.add_argument("--l1-result", type=Path)
    parser.add_argument("--l2-result", type=Path)
    parser.add_argument("--require-ready", action="store_true", help=argparse.SUPPRESS)
    args = parser.parse_args()
    if args.require_ready:
        if not args.answers.is_file():
            parser.exit(2, "尚無 answers.json；請先執行 make run 產生十題答案。\n")
        for level, expected in (("l1", 6), ("l2", 3)):
            try:
                score = score_check(level, args.l1_result if level == "l1" else args.l2_result)
            except (OSError, ValueError, RuntimeError) as exc:
                parser.exit(2, f"{level.upper()} 檢查無法完成（{exc}）；請先執行 make check L={level.upper()}。\n")
            if score["passed"] != expected:
                parser.exit(
                    2, f"{level.upper()} 尚未全過（{score['passed']}/{expected}）；"
                    f"請先執行 make check L={level.upper()} 並完成修復。\n",
                )
    try:
        token = read_token()
    except OSError as exc:
        parser.error(f"無法讀取課堂 token（{exc.strerror}）；請重新 make claim")
    questions = None
    if token:
        try:
            questions = remote_questions(token)
        except (HTTPError, URLError, TimeoutError, OSError, ValueError, TypeError) as exc:
            print(f"遠端題組取得失敗（{type(exc).__name__}），改用 questions.sample.json", file=sys.stderr)
    payload = build_payload(args.questions, args.answers, args.usage, args.l1_result, args.l2_result, questions)
    body = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.dry_run:
        print(body)
        return
    url = os.getenv("SUBMIT_URL") or (
        os.getenv("GATEWAY_URL", "").rstrip("/") + "/submit" if token else ""
    )
    class_code = os.getenv("CLASS_CODE", "")
    if not url or not payload["nickname"] or (not token and not class_code):
        parser.error("實送需設定 SUBMIT_URL 或 GATEWAY_URL、NICKNAME；未領 token 時另需 CLASS_CODE")
    try:
        require_https(url)
    except ValueError as exc:
        parser.error(str(exc))
    headers = {"Content-Type": "application/json"}
    if not token:
        # 舊式無 token 的 SUBMIT_URL 以班級代碼驗證；只放在請求本文頂層，不進入 payload／details。
        body = json.dumps({**payload, "class_code": class_code}, ensure_ascii=False, indent=2)
    if token:
        headers["Authorization"] = f"Bearer {token}"
        body = json.dumps({
            "level": "L3", "passed": payload["quality_passed"] == payload["question_count"],
            "tokens": payload["total_tokens"], "cost": payload["total_cost_usd"],
            "details": payload,
        }, ensure_ascii=False)
    request = Request(url, data=body.encode("utf-8"), headers=headers, method="POST")
    with urlopen(request, timeout=15) as response:
        print(f"提交成功：HTTP {response.status}")


if __name__ == "__main__":
    from hw.console import configure_console

    configure_console()
    main()
