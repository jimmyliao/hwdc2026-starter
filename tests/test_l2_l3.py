import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from scenarios import runner, tasks
from gateway.pricing import estimate_tokens
from submit import submit


def test_sample_has_ten_questions_and_long_context():
    questions = runner.load_questions(runner.SAMPLE_QUESTIONS)
    assert len(questions) == 10
    assert estimate_tokens(questions[-1]["question"]) > tasks.MAX_INPUT_TOKENS
    assert len({item["id"] for item in questions}) == 10


def test_task_uses_agent_public_api(monkeypatch):
    from agent import main as agent_main

    monkeypatch.setattr(agent_main, "ask", lambda question, tenant=None: (question, tenant))
    assert tasks.ask("問題", "team-a") == ("問題", "team-a")


def test_runner_records_ten_answers_and_usage_range(tmp_path, monkeypatch):
    monkeypatch.setenv("PROVIDER", "mock")
    monkeypatch.delenv("FALLBACK", raising=False)
    usage = tmp_path / "usage.jsonl"

    def fake_task(question, tenant="default"):
        with usage.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps({"tenant": tenant, "cost_usd": 0.001}) + "\n")
        return SimpleNamespace(text="星河", fallback_used=False)

    monkeypatch.setattr(runner, "run_task", fake_task)
    monkeypatch.setattr(runner, "run_with_fallback", fake_task)
    output = tmp_path / "answers.json"
    result = runner.run(runner.SAMPLE_QUESTIONS, output, usage)
    assert len(result["answers"]) == 10
    assert result["usage_start"] == 0
    assert result["usage_end"] == 10
    assert json.loads(output.read_text(encoding="utf-8")) == result


def test_runner_prints_tokens_and_cost(tmp_path, monkeypatch, capsys):
    usage = tmp_path / "usage.jsonl"
    usage.write_text(
        json.dumps({"input_tokens": 12, "output_tokens": 3, "cost_usd": 0.002}) + "\n",
        encoding="utf-8",
    )
    monkeypatch.setattr(runner, "run", lambda *args: {"answers": [{}] * 10, "usage_start": 0, "usage_end": 1})
    monkeypatch.setattr("sys.argv", ["runner", "--usage", str(usage)])
    runner.main()
    assert "總 token：15；總成本：$0.00200000" in capsys.readouterr().out


@pytest.mark.parametrize(("provider", "fallback"), [
    ("fast", ""), ("class", ""), ("mock", "class"), ("mock", "fast,mock"),
])
def test_runner_rejects_real_batch_before_writing(tmp_path, monkeypatch, provider, fallback):
    from gateway import llm

    monkeypatch.setenv("PROVIDER", provider)
    monkeypatch.setenv("FALLBACK", fallback)
    monkeypatch.delenv("INSTRUCTOR", raising=False)
    output = tmp_path / "answers.json"
    with pytest.raises(llm.ConfigError, match="只允許 mock.*費用"):
        runner.run(runner.SAMPLE_QUESTIONS, output, tmp_path / "usage.jsonl")
    assert not output.exists()


def test_runner_rejects_hardcoded_real_alias_and_allows_instructor(tmp_path, monkeypatch):
    from gateway import llm

    monkeypatch.setenv("PROVIDER", "mock")
    monkeypatch.delenv("FALLBACK", raising=False)
    monkeypatch.delenv("INSTRUCTOR", raising=False)
    monkeypatch.setattr(tasks, "ask", lambda *args, **kwargs: llm.chat([], alias="class"))
    with pytest.raises(llm.ConfigError, match="只允許 mock"):
        runner.run(runner.SAMPLE_QUESTIONS, tmp_path / "answers.json", tmp_path / "usage.jsonl")
    monkeypatch.setenv("INSTRUCTOR", "1")
    monkeypatch.setattr(runner, "run_task", lambda *args, **kwargs: "講師示範")
    monkeypatch.setattr(runner, "run_with_fallback", lambda *args, **kwargs: "講師示範")
    assert len(runner.run(runner.SAMPLE_QUESTIONS, tmp_path / "answers.json", tmp_path / "usage.jsonl")["answers"]) == 10


def test_submit_dry_run_scores_quality_and_cost(tmp_path, monkeypatch, capsys):
    usage = tmp_path / "usage.jsonl"
    usage.write_text(
        json.dumps({"cost_usd": 99}) + "\n"
        + "".join(json.dumps({"cost_usd": 0.001, "provider": "mock", "status": "ok"}) + "\n" for _ in range(10)),
        encoding="utf-8",
    )
    questions = runner.load_questions(runner.SAMPLE_QUESTIONS)
    answers = tmp_path / "answers.json"
    answers.write_text(
        json.dumps({
            "answers": [
                {"id": q["id"], "answer": " ".join(q["expected_keywords"])}
                for q in questions
            ],
            "usage_start": 1,
            "usage_end": 11,
        }, ensure_ascii=False),
        encoding="utf-8",
    )
    l1 = tmp_path / "l1.txt"
    l2_result = tmp_path / "l2.txt"
    l1.write_text("4/6", encoding="utf-8")
    l2_result.write_text("2/3", encoding="utf-8")
    monkeypatch.setattr(submit, "read_token", lambda: "")
    monkeypatch.setattr("sys.argv", [
        "submit", "--dry-run", "--answers", str(answers), "--usage", str(usage),
        "--l1-result", str(l1), "--l2-result", str(l2_result),
    ])
    monkeypatch.setattr(submit, "urlopen", lambda *args, **kwargs: pytest.fail("dry-run 不應送出網路請求"))
    submit.main()
    payload = json.loads(capsys.readouterr().out)
    assert payload["question_count"] == payload["quality_passed"] == 10
    assert payload["total_cost_usd"] == 0.01
    assert payload["l1"] == {"passed": 4, "total": 6}
    assert payload["l2"] == {"passed": 2, "total": 3}


def test_submit_rejects_missing_answers(tmp_path):
    with pytest.raises(FileNotFoundError):
        submit.build_payload(runner.SAMPLE_QUESTIONS, tmp_path / "missing.json", Path("unused"))


def test_make_submit_guides_missing_answers_and_checks(tmp_path, monkeypatch, capsys):
    answers = tmp_path / "answers.json"
    monkeypatch.setattr("sys.argv", ["submit", "--require-ready", "--dry-run", "--answers", str(answers)])
    with pytest.raises(SystemExit) as exc:
        submit.main()
    assert exc.value.code != 0
    assert "make run" in capsys.readouterr().err

    answers.write_text("{}", encoding="utf-8")
    l1 = tmp_path / "l1.txt"
    l1.write_text("0/6", encoding="utf-8")
    monkeypatch.setattr("sys.argv", [
        "submit", "--require-ready", "--dry-run", "--answers", str(answers),
        "--l1-result", str(l1),
    ])
    with pytest.raises(SystemExit) as exc:
        submit.main()
    assert exc.value.code != 0
    assert "make check L=L1" in capsys.readouterr().err

    l1.write_text("6/6", encoding="utf-8")
    l2 = tmp_path / "l2.txt"
    l2.write_text("0/3", encoding="utf-8")
    monkeypatch.setattr("sys.argv", [
        "submit", "--require-ready", "--dry-run", "--answers", str(answers),
        "--l1-result", str(l1), "--l2-result", str(l2),
    ])
    with pytest.raises(SystemExit) as exc:
        submit.main()
    assert exc.value.code != 0
    assert "make check L=L2" in capsys.readouterr().err
