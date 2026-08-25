"""公开、独立进程的生成失败事务 Harness。

模块顶层只导入标准库；生产路由与 FastAPI/SQLAlchemy 均在临时 runtime、
审计钩子和 fake 服务安装完成后延迟导入。
"""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
import os
import sys
import tempfile
import types
from pathlib import Path
from typing import Any


MODULE_PATH = Path(__file__).resolve()
EVAL_ROOT = MODULE_PATH.parent
PROJECT_ROOT = EVAL_ROOT.parents[1]
PUBLIC_FIXTURE = EVAL_ROOT / "fixtures" / "failure_transactions_public_v1.json"
DEFAULT_REPORT_DIR = EVAL_ROOT / "reports" / "public-failure-transactions"
REPORT_FILENAME = "failure_transactions_public_v1.json"
REPORT_SCHEMA = "papermind-failure-transactions-report-v1"
FIXTURE_SCHEMA = "papermind-failure-transactions-fixture-v1"
BENCHMARK_ID = "papermind-failure-transactions-public-v1"
CANARY = "synthetic-secret-canary"

_VIOLATION_KEYS = (
    "scenario_count_mismatch",
    "success_control_failure_count",
    "fake_call_contract_mismatch",
    "finished_on_failure_count",
    "multiple_terminal_count",
    "unsanitized_error_count",
    "assistant_rows_on_failure",
    "orphan_conversation_count",
    "message_count_mismatch",
    "regenerate_mutation_count",
    "rollback_failure_count",
    "log_canary_leak_count",
    "report_privacy_violations",
    "network_attempts",
    "subprocess_attempts",
    "private_path_attempts",
    "real_service_module_count",
)
_FAKE_SERVICE_MODULES = (
    "app.services.llm",
    "app.services.retrieval",
    "app.services.retrieval_pipeline",
    "app.services.memory_manager",
    "app.services.image_analyzer",
    "app.services.skills",
    "app.services.agent_graph",
)
_REPORT_KEYS = {
    "report_schema",
    "benchmark_id",
    "license",
    "synthetic",
    "fixture_sha256",
    "runner_sha256",
    "scenario_count",
    "scenarios",
    "overall",
    "offline_proof",
    "gate",
}
_SCENARIO_REPORT_KEYS = {
    "scenario_id",
    "operation",
    "failure",
    "passed",
    "terminal_count",
    "finished_count",
    "db_invariants_passed",
    "fake_calls",
}


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def build_failure_transaction_gate(counters: dict[str, int]) -> dict[str, Any]:
    """所有违规计数必须精确为零；未知输入不影响冻结检查集合。"""
    checks = {}
    for key in _VIOLATION_KEYS:
        actual = int(counters.get(key, 0))
        checks[key] = {"actual": actual, "threshold": 0, "passed": actual == 0}
    return {"passed": all(item["passed"] for item in checks.values()), "checks": checks}


def validate_public_report(report: dict[str, Any]) -> None:
    """严格白名单校验；正文、路径、异常与未知字段一律拒绝。"""
    if not isinstance(report, dict) or set(report) != _REPORT_KEYS:
        raise ValueError("失败事务报告顶层 schema 不兼容")
    if report.get("report_schema") != REPORT_SCHEMA:
        raise ValueError("失败事务报告版本不兼容")
    if report.get("benchmark_id") != BENCHMARK_ID:
        raise ValueError("失败事务 benchmark_id 不匹配")
    if report.get("license") != "CC0-1.0" or report.get("synthetic") is not True:
        raise ValueError("失败事务报告必须是公开合成基准")
    scenarios = report.get("scenarios")
    if not isinstance(scenarios, list) or len(scenarios) != report.get("scenario_count"):
        raise ValueError("失败事务场景计数不匹配")
    if any(not isinstance(item, dict) or set(item) != _SCENARIO_REPORT_KEYS for item in scenarios):
        raise ValueError("失败事务场景报告含未知字段")
    if set(report.get("overall") or {}) != set(_VIOLATION_KEYS):
        raise ValueError("失败事务 overall schema 不兼容")
    if set(report.get("offline_proof") or {}) != {
        "network_attempts",
        "subprocess_attempts",
        "private_path_attempts",
        "real_service_module_count",
    }:
        raise ValueError("失败事务离线证明 schema 不兼容")

    rendered = json.dumps(report, ensure_ascii=False, sort_keys=True)
    forbidden_tokens = (
        CANARY,
        "synthetic-question",
        "synthetic-answer",
        "Traceback",
        "api_key",
        "https://",
        "http://",
        str(PROJECT_ROOT.resolve()),
    )
    if any(token in rendered for token in forbidden_tokens):
        raise ValueError("失败事务报告包含隐私或内容字段")


def _load_fixture() -> tuple[dict[str, Any], bytes]:
    fixture_bytes = PUBLIC_FIXTURE.read_bytes()
    fixture = json.loads(fixture_bytes)
    if set(fixture) != {"fixture_schema", "benchmark_id", "license", "synthetic", "scenarios"}:
        raise ValueError("失败事务 fixture 顶层 schema 不兼容")
    if fixture["fixture_schema"] != FIXTURE_SCHEMA or fixture["benchmark_id"] != BENCHMARK_ID:
        raise ValueError("失败事务 fixture 版本不兼容")
    if fixture["license"] != "CC0-1.0" or fixture["synthetic"] is not True:
        raise ValueError("失败事务 fixture 必须是 CC0 合成数据")
    scenarios = fixture.get("scenarios")
    ids = [item.get("scenario_id") for item in scenarios or []]
    if not scenarios or any(not isinstance(item, dict) for item in scenarios):
        raise ValueError("失败事务 fixture scenarios 不得为空")
    if any(not isinstance(item, str) or not item for item in ids) or len(ids) != len(set(ids)):
        raise ValueError("失败事务 scenario_id 必须唯一")
    return fixture, fixture_bytes


def _prepare_environment(runtime_root: Path) -> None:
    runtime_root.mkdir(parents=True, exist_ok=True)
    (runtime_root / "config.yaml").write_text("{}\n", encoding="utf-8")
    os.environ["PAPERMIND_DATA_DIR"] = str(runtime_root)
    for key in (
        "OPENAI_API_KEY",
        "KIMI_API_KEY",
        "MOONSHOT_API_KEY",
        "LANGFUSE_PUBLIC_KEY",
        "LANGFUSE_SECRET_KEY",
    ):
        os.environ.pop(key, None)


def _install_audit(runtime_root: Path) -> dict[str, int]:
    counters = {"network_attempts": 0, "subprocess_attempts": 0, "private_path_attempts": 0}
    forbidden = [
        PROJECT_ROOT / name
        for name in (
            "papers",
            "notes",
            "summaries",
            "my-thesis",
            "data",
            "vector_db",
            "logs",
            "backups",
            "cache",
            "config.yaml",
        )
    ] + [EVAL_ROOT / "private"]
    resolved_forbidden = [path.resolve(strict=False) for path in forbidden]
    allowed_runtime = runtime_root.resolve(strict=False)
    allowed_reports = (EVAL_ROOT / "reports").resolve(strict=False)

    def inspect_path(raw: object) -> None:
        if not isinstance(raw, (str, bytes, os.PathLike)):
            return
        try:
            path = Path(raw).resolve(strict=False)
        except (OSError, TypeError, ValueError):
            return
        if path == allowed_runtime or allowed_runtime in path.parents:
            return
        if path == allowed_reports or allowed_reports in path.parents:
            return
        if any(path == item or item in path.parents for item in resolved_forbidden):
            counters["private_path_attempts"] += 1
            raise RuntimeError("公开失败事务 Harness 禁止访问私有路径")

    def hook(event: str, args: tuple[object, ...]) -> None:
        if event in {
            "socket.connect",
            "socket.connect_ex",
            "socket.getaddrinfo",
            "socket.gethostbyaddr",
            "socket.gethostbyname",
            "socket.gethostbyname_ex",
            "socket.sendto",
            "socket.bind",
        }:
            counters["network_attempts"] += 1
            raise RuntimeError("公开失败事务 Harness 禁止网络")
        if event.startswith("subprocess.") or event in {
            "os.system",
            "os.posix_spawn",
            "os.spawn",
            "os.spawnl",
            "os.spawnle",
            "os.spawnlp",
            "os.spawnlpe",
            "os.spawnv",
            "os.spawnve",
            "os.spawnvp",
            "os.spawnvpe",
        }:
            counters["subprocess_attempts"] += 1
            raise RuntimeError("公开失败事务 Harness 禁止子进程")
        if event == "open" and args:
            inspect_path(args[0])
        elif event in {"os.listdir", "os.scandir", "sqlite3.connect"} and args:
            inspect_path(args[0])

    sys.addaudithook(hook)
    return counters


class _HarnessState:
    scenario_id = ""
    fake_calls = 0


def _fake_module(name: str, **attributes: object) -> types.ModuleType:
    module = types.ModuleType(name)
    module._PAPERMIND_HARNESS_FAKE = True
    for key, value in attributes.items():
        setattr(module, key, value)
    sys.modules[name] = module
    return module


def _install_fake_services() -> None:
    from app.services.generation_guardrails import build_rag_prompt, verify_citations

    class LLMGenerationError(RuntimeError):
        pass

    class FakeLLM:
        async def chat_stream(self, messages, enable_web_search=False):
            _HarnessState.fake_calls += 1
            yield "synthetic-answer"
            if _HarnessState.scenario_id == "chat-stream-failure":
                raise RuntimeError(CANARY)
            if _HarnessState.scenario_id == "chat-cancelled":
                raise asyncio.CancelledError()

        async def chat_completion(self, messages):
            _HarnessState.fake_calls += 1
            return "synthetic-answer"

    def is_llm_error_response(value: object) -> bool:
        return isinstance(value, str) and value.startswith("[调用 LLM 出错")

    _fake_module(
        "app.services.llm",
        LLMGenerationError=LLMGenerationError,
        is_llm_error_response=is_llm_error_response,
        llm_service=FakeLLM(),
    )
    _fake_module(
        "app.services.retrieval",
        get_vector_store=lambda: types.SimpleNamespace(available=lambda: False),
    )

    class FakePipeline:
        def __init__(self, *args, **kwargs):
            pass

        def search(self, *args, **kwargs):
            return []

    _fake_module("app.services.retrieval_pipeline", RetrievalPipeline=FakePipeline)

    class FakeMemoryManager:
        def __init__(self, db):
            self.db = db

        def build_memory_context(self):
            return ""

        async def update_short_term_memory(self, conversation_id):
            return None

    _fake_module("app.services.memory_manager", MemoryManager=FakeMemoryManager)
    _fake_module(
        "app.services.image_analyzer",
        image_analyzer_service=types.SimpleNamespace(),
    )
    _fake_module("app.services.skills", list_skills=lambda: [])

    def run_pre_orchestration(db, conversation_id, user_message, **kwargs):
        from app.models import Message

        total = db.query(Message).filter(Message.conversation_id == conversation_id).count()
        return {
            "messages": [{"role": "user", "content": user_message}],
            "context_chunks": [],
            "web_search_enabled": False,
            "history_total": total,
        }

    _fake_module(
        "app.services.agent_graph",
        NO_RETRIEVAL_GUARD="synthetic-no-retrieval",
        SYSTEM_PROMPT="synthetic-system",
        build_rag_prompt=build_rag_prompt,
        run_pre_orchestration=run_pre_orchestration,
        verify_citations=verify_citations,
    )

    async def deep_plan(topic):
        _HarnessState.fake_calls += 1
        if _HarnessState.scenario_id == "deep-review-plan-failure":
            raise RuntimeError(CANARY)
        return ["synthetic-subquestion"]

    async def deep_execute(question, db=None):
        _HarnessState.fake_calls += 1
        return {"question": question, "answer": "synthetic-subanswer", "citations": []}

    async def deep_synthesize(topic, sub_answers):
        _HarnessState.fake_calls += 1
        return {"content": "synthetic-answer", "citations": []}

    _fake_module(
        "app.services.deep_review",
        plan=deep_plan,
        execute=deep_execute,
        synthesize=deep_synthesize,
    )


def _parse_frames(response_text: str) -> list[dict[str, Any]]:
    frames = []
    for line in response_text.splitlines():
        if line.startswith("data: "):
            frames.append(json.loads(line[6:]))
    return frames


def _scenario_database(db_path: Path, fail_final_commit: bool):
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import Session, sessionmaker
    from sqlalchemy.pool import NullPool

    connection_ids: set[int] = set()
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )

    @event.listens_for(engine, "connect")
    def configure(dbapi_connection, connection_record):
        connection_ids.add(id(dbapi_connection))
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA busy_timeout=5000")
        cursor.close()

    with engine.begin() as connection:
        mode = connection.exec_driver_sql("PRAGMA journal_mode=WAL").scalar()
        if str(mode).lower() != "wal":
            raise RuntimeError("文件 SQLite 未启用 WAL")

    class FailingSession(Session):
        def commit(self):
            if fail_final_commit:
                raise RuntimeError(CANARY)
            return super().commit()

    normal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    finalizer = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=engine,
        class_=FailingSession if fail_final_commit else Session,
    )
    return engine, normal, finalizer, connection_ids


def _run_scenario(
    scenario: dict[str, Any], runtime_root: Path
) -> tuple[dict[str, Any], dict[str, int]]:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient

    import app.database as database_module
    from app.database import Base, get_db
    from app.models import Conversation, Message
    from app.routers import chat

    scenario_id = scenario["scenario_id"]
    _HarnessState.scenario_id = scenario_id
    _HarnessState.fake_calls = 0
    fail_commit = scenario["failure"] == "commit"
    db_path = runtime_root / f"{scenario_id}.sqlite3"
    engine, NormalSession, FinalizerSession, connection_ids = _scenario_database(
        db_path, fail_commit
    )
    Base.metadata.create_all(bind=engine)
    database_module.SessionLocal = FinalizerSession

    original_regenerate = None
    if scenario["operation"] == "regenerate":
        with NormalSession() as seed_db:
            conv = Conversation(title="synthetic", message_count=2)
            seed_db.add(conv)
            seed_db.flush()
            seed_db.add(Message(
                conversation_id=conv.id,
                role="user",
                content="synthetic-question",
                citations=[],
            ))
            assistant = Message(
                conversation_id=conv.id,
                role="assistant",
                content="synthetic-original",
                citations=[{"source": "p1_c0", "paper_id": 1}],
                revision=0,
            )
            seed_db.add(assistant)
            seed_db.commit()
            seed_db.refresh(assistant)
            conversation_id, message_id = conv.id, assistant.id
            original_regenerate = (assistant.content, assistant.citations, assistant.revision)

    app = FastAPI()
    app.include_router(chat.router, prefix="/api/chat")

    def override_get_db():
        with NormalSession() as session:
            yield session

    app.dependency_overrides[get_db] = override_get_db
    client = TestClient(app)
    if scenario["operation"] == "chat":
        response = client.post(
            "/api/chat",
            json={"message": "synthetic-question", "stream": True},
        )
    elif scenario["operation"] == "deep-review":
        response = client.post(
            "/api/chat/deep-review",
            json={"topic": "synthetic-question"},
        )
    else:
        response = client.post(
            f"/api/chat/conversations/{conversation_id}/messages/{message_id}/regenerate",
            json={"expected_revision": 0},
        )
    frames = _parse_frames(response.text)
    terminals = [frame for frame in frames if frame.get("finished") is True or "error" in frame]
    finished_count = sum(frame.get("finished") is True for frame in frames)

    with NormalSession() as verify_db:
        conversations = verify_db.query(Conversation).all()
        messages = verify_db.query(Message).all()
        assistants = [message for message in messages if message.role == "assistant"]
        count_mismatches = sum(
            conversation.message_count
            != verify_db.query(Message).filter(Message.conversation_id == conversation.id).count()
            for conversation in conversations
        )
        regenerate_changed = 0
        if original_regenerate is not None:
            saved = verify_db.query(Message).filter(Message.id == message_id).one()
            regenerate_changed = int(
                (saved.content, saved.citations, saved.revision) != original_regenerate
            )

    is_success = scenario["failure"] == "none"
    db_ok = (
        len(conversations) == scenario["expected_conversations"]
        and len(messages) == scenario["expected_messages"]
        and len(assistants) == scenario["expected_assistants"]
        and count_mismatches == 0
    )
    terminal_ok = (
        finished_count == 1 and len(terminals) == 1
        if is_success
        else finished_count == 0 and len(terminals) <= 1
    )
    fake_ok = _HarnessState.fake_calls == scenario["expected_llm_calls"]
    response_clean = CANARY not in response.text and "Traceback" not in response.text
    passed = db_ok and terminal_ok and fake_ok and response_clean and len(connection_ids) >= 2

    violations = {
        "success_control_failure_count": int(is_success and not passed),
        "fake_call_contract_mismatch": int(not fake_ok),
        "finished_on_failure_count": 0 if is_success else finished_count,
        "multiple_terminal_count": max(0, len(terminals) - 1),
        "unsanitized_error_count": int(not response_clean),
        "assistant_rows_on_failure": 0 if is_success else max(
            0, len(assistants) - scenario["expected_assistants"]
        ),
        "orphan_conversation_count": int(
            scenario["operation"] == "deep-review"
            and not is_success
            and len(conversations) != scenario["expected_conversations"]
        ),
        "message_count_mismatch": count_mismatches,
        "regenerate_mutation_count": regenerate_changed,
        "rollback_failure_count": int(not db_ok),
    }
    report = {
        "scenario_id": scenario_id,
        "operation": scenario["operation"],
        "failure": scenario["failure"],
        "passed": passed,
        "terminal_count": len(terminals),
        "finished_count": finished_count,
        "db_invariants_passed": db_ok,
        "fake_calls": _HarnessState.fake_calls,
    }
    engine.dispose()
    return report, violations


def _report_dir(path: Path) -> Path:
    target = path.resolve()
    reports_root = (EVAL_ROOT / "reports").resolve()
    if target != reports_root and reports_root not in target.parents:
        raise ValueError("失败事务报告只能写入 backend/eval/reports")
    return target


def run_public_failure_transactions(report_dir: Path) -> tuple[dict[str, Any], Path]:
    fixture, fixture_bytes = _load_fixture()
    with tempfile.TemporaryDirectory(prefix="papermind-failure-harness-") as temp_name:
        runtime_root = Path(temp_name)
        _prepare_environment(runtime_root)
        audit = _install_audit(runtime_root)
        _install_fake_services()

        scenario_reports = []
        aggregate = {key: 0 for key in _VIOLATION_KEYS}
        for scenario in fixture["scenarios"]:
            scenario_report, violations = _run_scenario(scenario, runtime_root)
            scenario_reports.append(scenario_report)
            for key, value in violations.items():
                aggregate[key] += value

        aggregate["scenario_count_mismatch"] = int(
            len(scenario_reports) != len(fixture["scenarios"])
        )
        real_modules = [
            name
            for name in _FAKE_SERVICE_MODULES
            if name in sys.modules
            and not getattr(sys.modules[name], "_PAPERMIND_HARNESS_FAKE", False)
        ]
        aggregate["network_attempts"] = audit["network_attempts"]
        aggregate["subprocess_attempts"] = audit["subprocess_attempts"]
        aggregate["private_path_attempts"] = audit["private_path_attempts"]
        aggregate["real_service_module_count"] = len(real_modules)

        log_path = runtime_root / "logs" / "app.log"
        aggregate["log_canary_leak_count"] = int(
            log_path.exists() and CANARY in log_path.read_text(encoding="utf-8")
        )
        offline_proof = {
            "network_attempts": audit["network_attempts"],
            "subprocess_attempts": audit["subprocess_attempts"],
            "private_path_attempts": audit["private_path_attempts"],
            "real_service_module_count": len(real_modules),
        }
        report = {
            "report_schema": REPORT_SCHEMA,
            "benchmark_id": BENCHMARK_ID,
            "license": fixture["license"],
            "synthetic": True,
            "fixture_sha256": _sha256(fixture_bytes),
            "runner_sha256": _sha256(MODULE_PATH.read_bytes()),
            "scenario_count": len(scenario_reports),
            "scenarios": scenario_reports,
            "overall": aggregate,
            "offline_proof": offline_proof,
            "gate": build_failure_transaction_gate(aggregate),
        }
        try:
            validate_public_report(report)
        except ValueError:
            aggregate["report_privacy_violations"] += 1
            report["gate"] = build_failure_transaction_gate(aggregate)
            raise

        target_dir = _report_dir(report_dir)
        target_dir.mkdir(parents=True, exist_ok=True)
        target = target_dir / REPORT_FILENAME
        fd, temp_path = tempfile.mkstemp(prefix=f".{REPORT_FILENAME}.", dir=target_dir)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump(report, handle, ensure_ascii=False, indent=2, sort_keys=True)
                handle.write("\n")
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temp_path, target)
        except Exception:
            Path(temp_path).unlink(missing_ok=True)
            raise
        return report, target


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="PaperMind 公开生成失败事务 Harness")
    parser.add_argument("--report-dir", type=Path, default=DEFAULT_REPORT_DIR)
    args = parser.parse_args(argv)
    try:
        report, report_path = run_public_failure_transactions(args.report_dir)
    except Exception as exc:
        print(f"failure transaction harness invalid: {type(exc).__name__}", file=sys.stderr)
        return 2
    print(
        "failure transactions "
        f"scenarios={report['scenario_count']} "
        f"gate={'PASS' if report['gate']['passed'] else 'FAIL'} "
        f"report={report_path.name}"
    )
    return 0 if report["gate"]["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
