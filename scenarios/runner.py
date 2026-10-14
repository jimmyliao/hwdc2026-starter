"""執行十道題，記錄答案與對應的 usage 範圍。"""

import argparse
import json
import os
import sys
from contextlib import nullcontext
from pathlib import Path

from dotenv import load_dotenv
from gateway import llm
from scenarios.tasks import run_task, run_with_fallback


SAMPLE_QUESTIONS = Path(__file__).resolve().parent.parent / "questions.sample.json"


def load_questions(path: Path) -> list[dict]:
    questions = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(questions, list) or len(questions) != 10:
        raise ValueError("題組必須剛好有 10 題")
    ids = [question["id"] for question in questions]
    if len(set(ids)) != 10:
        raise ValueError("題目 id 不可重複")
    for question in questions:
        if not isinstance(question.get("question"), str) or not question["question"]:
            raise ValueError("每題都需要非空的 question")
        if not isinstance(question.get("expected_keywords"), list) or not question["expected_keywords"] or not all(
            isinstance(word, str) and word for word in question["expected_keywords"]
        ):
            raise ValueError("每題都需要非空的 expected_keywords")
        question["question"] = question["question"].replace(
            "{long_context}", ("背景資料與主題無關。" * 1400) + "主題標記：星河。"
        )
    return questions


def usage_count(path: Path) -> int:
    if not path.exists():
        return 0
    with path.open(encoding="utf-8") as handle:
        return sum(1 for _ in handle)


def run(questions_path: Path, output: Path, usage_path: Path) -> dict:
    load_dotenv(".env")
    provider = os.getenv("PROVIDER", "mock")
    fallback = [name.strip() for name in os.getenv("FALLBACK", "").split(",") if name.strip()]
    if os.getenv("INSTRUCTOR") != "1" and (provider != "mock" or any(name != "mock" for name in fallback)):
        raise llm.ConfigError("十題批次練習只允許 mock；真實 provider 請改用單題 make ask（或 uv run hw ask <問題>），以避免無謂請求與費用")
    questions = load_questions(questions_path)
    start = usage_count(usage_path)
    answers = []
    previous_usage = os.environ.get("USAGE_LOG")
    os.environ["USAGE_LOG"] = str(usage_path)
    try:
        with llm.mock_only() if os.getenv("INSTRUCTOR") != "1" else nullcontext():
            for question in questions:
                task = run_with_fallback if question.get("fallback") else run_task
                answer = task(question["question"], tenant=question.get("tenant", "default"))
                answers.append({
                    "id": question["id"],
                    "answer": answer if isinstance(answer, str) else answer.text,
                })
    finally:
        if previous_usage is None:
            os.environ.pop("USAGE_LOG", None)
        else:
            os.environ["USAGE_LOG"] = previous_usage
    result = {"answers": answers, "usage_start": start, "usage_end": usage_count(usage_path)}
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return result


def main() -> None:
    parser = argparse.ArgumentParser(description="執行十道離線示範題")
    parser.add_argument("--questions", type=Path, default=SAMPLE_QUESTIONS)
    parser.add_argument("--output", type=Path, default=Path("answers.json"))
    parser.add_argument("--usage", type=Path, default=Path(os.getenv("USAGE_LOG", "usage.jsonl")))
    args = parser.parse_args()
    try:
        result = run(args.questions, args.output, args.usage)
    except llm.ConfigError as exc:
        parser.exit(2, f"{exc}\n")
    print(f"已記錄 {len(result['answers'])}/10 題答案：{args.output}")
    with args.usage.open(encoding="utf-8") as handle:
        usage = [
            json.loads(line) for index, line in enumerate(handle)
            if result["usage_start"] <= index < result["usage_end"]
        ]
    tokens = sum(row["input_tokens"] + row["output_tokens"] for row in usage)
    cost = sum(row["cost_usd"] for row in usage)
    print(f"這 10 題總 token：{tokens}；總成本：${cost:.8f} USD（mock 教學用固定價格）")


if __name__ == "__main__":
    from hw.console import configure_console

    configure_console()
    main()
