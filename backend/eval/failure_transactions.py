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
import threading
import types
from pathlib import Path
from typing import Any


MODULE_PATH = Path(__file__).resolve()
EVAL_ROOT = MODULE_PATH.parent
PROJECT_ROOT = EVAL_ROOT.parents[1]
PUBLIC_FIXTURE = EVAL_ROOT / "fixtures" / "failure_transactions_public_v2.json"
DEFAULT_REPORT_DIR = EVAL_ROOT / "reports" / "public-failure-transactions"
REPORT_FILENAME = "failure_transactions_public_v2.json"
REPORT_SCHEMA = "papermind-failure-transactions-report-v2"
FIXTURE_SCHEMA = "papermind-failure-transactions-fixture-v2"
BENCHMARK_ID = "papermind-failure-transactions-public-v2"
CANARY = "synthetic-secret-canary"

_VIOLATION_KEYS = (
    "scenario_count_mismatch",
    "scenario_failure_count",
    "active_regeneration_leak_count",
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
    "request_contract_mismatch",
    "error_code_mismatch",
    "coordination_timeout_count",
    "worker_exception_count",
    "live_worker_count",
    "unexpected_active_key_count",
    "active_409_reason_mismatch_count",
    "secondary_dependency_call_count",
    "external_mutation_commit_failure_count",
    "external_state_overwrite_count",
    "target_resurrection_count",
    "cancel_release_failure_count",
    "retry_failure_count",
    "terminal_order_violation_count",
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
    "app.services.deep_review",
)
_FORBIDDEN_REAL_MODULE_PREFIXES = (
    "app.main",
    "app.services.embedding",
    "app.services.web_search",
    "openai",
    "chromadb",
    "sentence_transformers",
    "torch",
    "transformers",
    "tokenizers",
    "langfuse",
)
_FIXTURE_SCENARIO_KEYS = {
    "scenario_id",
    "operation",
    "failure",
    "expected_terminal",
    "expected_error_code",
    "expected_http_status",
    "expected_peer_http_status",
    "expected_retry_http_status",
    "expected_request_count",
    "expected_llm_calls",
    "expected_retrieval_calls",
    "expected_deep_review_calls",
    "expected_conversations",
    "expected_messages",
    "expected_assistants",
    "expected_revision",
}
_REPORT_KEYS = {
    "report_schema",
    "benchmark_id",
    "license",
    "synthetic",
    "fixture_sha256",
    "runner_sha256",
    "implementation_sha256",
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
    "terminal",
    "error_code",
    "passed",
    "terminal_count",
    "finished_count",
    "db_invariants_passed",
    "fake_calls",
    "fake_llm_calls",
    "fake_retrieval_calls",
    "fake_deep_review_calls",
    "http_status",
    "peer_http_status",
    "retry_http_status",
    "request_count",
    "retry_finished_count",
    "coordination_verified",
    "external_commit_verified",
    "target_state_verified",
    "active_release_verified",
    "worker_join_verified",
}
_EXPECTED_SCENARIOS = {
    "chat-success-control": ("chat", "none", "finished", None, 200, None, None, 1, 1, 1, 0, 1, 2, 1, None),
    "chat-stream-failure": ("chat", "stream", "error", "llm_generation_failed", 200, None, None, 1, 1, 1, 0, 1, 1, 0, None),
    "chat-cancelled": ("chat", "cancel", "none", None, 200, None, None, 1, 1, 1, 0, 1, 1, 0, None),
    "chat-assistant-commit-failure": ("chat", "commit", "error", "finalization_failed", 200, None, None, 1, 1, 1, 0, 1, 1, 0, None),
    "deep-review-plan-failure": ("deep-review", "plan", "error", "deep_review_plan_failed", 200, None, None, 1, 0, 0, 1, 0, 0, 0, None),
    "deep-review-commit-failure": ("deep-review", "commit", "error", "finalization_failed", 200, None, None, 1, 0, 1, 3, 0, 0, 0, None),
    "regenerate-commit-failure": ("regenerate", "commit", "error", "finalization_failed", 200, None, None, 1, 1, 1, 0, 1, 2, 1, 0),
    "regenerate-active-second-request": ("regenerate", "active-conflict", "finished", None, 200, 409, None, 2, 1, 1, 0, 1, 2, 1, 1),
    "regenerate-external-revision-conflict": ("regenerate", "external-revision", "error", "regenerate_conflict", 200, None, None, 1, 1, 1, 0, 1, 2, 1, 1),
    "regenerate-external-delete": ("regenerate", "external-delete", "error", "regenerate_target_missing", 200, 204, None, 2, 1, 1, 0, 1, 1, 0, None),
    "regenerate-cancel-release-retry": ("regenerate", "cancel-retry", "none", None, 200, None, 200, 2, 2, 2, 0, 1, 2, 1, 1),
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
    if report.get("scenario_count") != len(_EXPECTED_SCENARIOS):
        raise ValueError("失败事务场景数与冻结契约不匹配")
    if any(not isinstance(item, dict) or set(item) != _SCENARIO_REPORT_KEYS for item in scenarios):
        raise ValueError("失败事务场景报告含未知字段")
    if [item.get("scenario_id") for item in scenarios] != list(_EXPECTED_SCENARIOS):
        raise ValueError("失败事务报告场景集合或顺序已漂移")
    for key in ("fixture_sha256", "runner_sha256", "implementation_sha256"):
        value = report.get(key)
        if not isinstance(value, str) or len(value) != 64 or any(
            char not in "0123456789abcdef" for char in value
        ):
            raise ValueError("失败事务报告 SHA256 格式无效")
    allowed_error_codes = {
        None,
        "llm_generation_failed",
        "finalization_failed",
        "deep_review_plan_failed",
        "regenerate_conflict",
        "regenerate_target_missing",
    }
    for item in scenarios:
        expected = _EXPECTED_SCENARIOS[item["scenario_id"]]
        if item["operation"] != expected[0] or item["failure"] != expected[1]:
            raise ValueError("失败事务场景元数据与冻结契约不匹配")
        if item["terminal"] not in {"finished", "error", "none", "invalid"}:
            raise ValueError("失败事务场景终态枚举无效")
        if item["error_code"] not in allowed_error_codes:
            raise ValueError("失败事务场景错误码枚举无效")
        for key in (
            "passed",
            "db_invariants_passed",
            "coordination_verified",
            "external_commit_verified",
            "target_state_verified",
            "active_release_verified",
            "worker_join_verified",
        ):
            if not isinstance(item[key], bool):
                raise ValueError("失败事务场景布尔字段类型无效")
        for key in (
            "terminal_count",
            "finished_count",
            "fake_calls",
            "fake_llm_calls",
            "fake_retrieval_calls",
            "fake_deep_review_calls",
            "http_status",
            "request_count",
            "retry_finished_count",
        ):
            if isinstance(item[key], bool) or not isinstance(item[key], int) or item[key] < 0:
                raise ValueError("失败事务场景计数字段类型无效")
        for key in ("peer_http_status", "retry_http_status"):
            value = item[key]
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 100
            ):
                raise ValueError("失败事务场景可选状态码类型无效")
        if item["fake_calls"] != (
            item["fake_llm_calls"]
            + item["fake_retrieval_calls"]
            + item["fake_deep_review_calls"]
        ):
            raise ValueError("失败事务场景 fake 调用汇总不一致")
        if item["passed"]:
            expected_terminal_count = int(expected[2] != "none")
            expected_finished_count = int(expected[2] == "finished")
            expected_retry_finished = int(expected[6] is not None)
            expected_coordination = item["scenario_id"] in {
                "regenerate-active-second-request",
                "regenerate-external-revision-conflict",
                "regenerate-external-delete",
            }
            expected_external = item["scenario_id"] in {
                "regenerate-external-revision-conflict",
                "regenerate-external-delete",
            }
            passed_contract = (
                item["terminal"] == expected[2]
                and item["error_code"] == expected[3]
                and item["http_status"] == expected[4]
                and item["peer_http_status"] == expected[5]
                and item["retry_http_status"] == expected[6]
                and item["request_count"] == expected[7]
                and item["fake_llm_calls"] == expected[8]
                and item["fake_retrieval_calls"] == expected[9]
                and item["fake_deep_review_calls"] == expected[10]
                and item["terminal_count"] == expected_terminal_count
                and item["finished_count"] == expected_finished_count
                and item["retry_finished_count"] == expected_retry_finished
                and item["db_invariants_passed"]
                and item["coordination_verified"] is expected_coordination
                and item["external_commit_verified"] is expected_external
                and item["target_state_verified"]
                and item["active_release_verified"]
                and item["worker_join_verified"]
            )
            if not passed_contract:
                raise ValueError("失败事务场景 PASS 与冻结行为证据不一致")
    if set(report.get("overall") or {}) != set(_VIOLATION_KEYS):
        raise ValueError("失败事务 overall schema 不兼容")
    if report["overall"]["scenario_failure_count"] != sum(
        not item["passed"] for item in scenarios
    ):
        raise ValueError("失败事务失败场景汇总不一致")
    if set(report.get("offline_proof") or {}) != {
        "fake_llm_calls",
        "fake_retrieval_calls",
        "fake_deep_review_calls",
        "request_count",
        "peer_request_count",
        "retry_request_count",
        "external_commit_count",
        "coordinated_scenario_count",
        "network_attempts",
        "subprocess_attempts",
        "private_path_attempts",
        "real_service_module_count",
    }:
        raise ValueError("失败事务离线证明 schema 不兼容")
    proof = report["offline_proof"]
    sums = {
        "fake_llm_calls": sum(item["fake_llm_calls"] for item in scenarios),
        "fake_retrieval_calls": sum(item["fake_retrieval_calls"] for item in scenarios),
        "fake_deep_review_calls": sum(item["fake_deep_review_calls"] for item in scenarios),
        "request_count": sum(item["request_count"] for item in scenarios),
        "peer_request_count": sum(item["peer_http_status"] is not None for item in scenarios),
        "retry_request_count": sum(item["retry_http_status"] is not None for item in scenarios),
        "external_commit_count": sum(item["external_commit_verified"] for item in scenarios),
        "coordinated_scenario_count": sum(item["coordination_verified"] for item in scenarios),
    }
    if any(proof[key] != value for key, value in sums.items()):
        raise ValueError("失败事务离线证明与场景求和不一致")
    expected_gate = build_failure_transaction_gate(report["overall"])
    if report.get("gate") != expected_gate:
        raise ValueError("失败事务 Gate 与 overall 不一致")

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
    if ids != list(_EXPECTED_SCENARIOS):
        raise ValueError("失败事务 fixture 场景集合或顺序已漂移")
    for scenario in scenarios:
        if set(scenario) != _FIXTURE_SCENARIO_KEYS:
            raise ValueError("失败事务 fixture 场景 schema 不兼容")
        contract = (
            scenario["operation"],
            scenario["failure"],
            scenario["expected_terminal"],
            scenario["expected_error_code"],
            scenario["expected_http_status"],
            scenario["expected_peer_http_status"],
            scenario["expected_retry_http_status"],
            scenario["expected_request_count"],
            scenario["expected_llm_calls"],
            scenario["expected_retrieval_calls"],
            scenario["expected_deep_review_calls"],
            scenario["expected_conversations"],
            scenario["expected_messages"],
            scenario["expected_assistants"],
            scenario["expected_revision"],
        )
        if contract != _EXPECTED_SCENARIOS[scenario["scenario_id"]]:
            raise ValueError("失败事务 fixture 场景契约已漂移")
        for key in (
            "expected_http_status",
            "expected_request_count",
            "expected_llm_calls",
            "expected_retrieval_calls",
            "expected_deep_review_calls",
            "expected_conversations",
            "expected_messages",
            "expected_assistants",
        ):
            value = scenario[key]
            if isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("失败事务 fixture 计数必须是非负整数")
        for key in ("expected_peer_http_status", "expected_retry_http_status"):
            value = scenario[key]
            if value is not None and (
                isinstance(value, bool) or not isinstance(value, int) or value < 100
            ):
                raise ValueError("失败事务 fixture 可选状态码无效")
        revision = scenario["expected_revision"]
        if revision is not None and (
            isinstance(revision, bool) or not isinstance(revision, int) or revision < 0
        ):
            raise ValueError("失败事务 fixture revision 无效")
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
        "PAPERMIND_LANGFUSE_PUBLIC_KEY",
        "PAPERMIND_LANGFUSE_SECRET_KEY",
        "HTTP_PROXY",
        "HTTPS_PROXY",
        "ALL_PROXY",
        "NO_PROXY",
    ):
        os.environ.pop(key, None)


def _install_audit(runtime_root: Path) -> dict[str, int]:
    counters = {"network_attempts": 0, "subprocess_attempts": 0, "private_path_attempts": 0}
    counter_lock = threading.Lock()
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
            with counter_lock:
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
            with counter_lock:
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
            with counter_lock:
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
    llm_calls = 0
    retrieval_calls = 0
    deep_review_calls = 0
    coordination_timeouts = 0
    leader_after_delta = threading.Event()
    release_leader = threading.Event()
    lock = threading.Lock()

    @classmethod
    def reset(cls, scenario_id: str) -> None:
        with cls.lock:
            cls.scenario_id = scenario_id
            cls.llm_calls = 0
            cls.retrieval_calls = 0
            cls.deep_review_calls = 0
            cls.coordination_timeouts = 0
        cls.leader_after_delta = threading.Event()
        cls.release_leader = threading.Event()

    @classmethod
    def increment(cls, key: str) -> int:
        with cls.lock:
            value = getattr(cls, key) + 1
            setattr(cls, key, value)
            return value

    @classmethod
    def snapshot(cls) -> dict[str, int]:
        with cls.lock:
            return {
                "llm_calls": cls.llm_calls,
                "retrieval_calls": cls.retrieval_calls,
                "deep_review_calls": cls.deep_review_calls,
                "coordination_timeouts": cls.coordination_timeouts,
            }

    @classmethod
    async def coordinate_after_delta(cls) -> None:
        cls.leader_after_delta.set()
        loop = asyncio.get_running_loop()
        deadline = loop.time() + 5.0
        while not cls.release_leader.is_set():
            if loop.time() >= deadline:
                cls.increment("coordination_timeouts")
                raise RuntimeError("coordination_timeout")
            await asyncio.sleep(0.005)


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
            call_index = _HarnessState.increment("llm_calls")
            yield "synthetic-answer"
            if _HarnessState.scenario_id == "chat-stream-failure":
                raise RuntimeError(CANARY)
            if _HarnessState.scenario_id == "chat-cancelled":
                raise asyncio.CancelledError()
            if _HarnessState.scenario_id in {
                "regenerate-active-second-request",
                "regenerate-external-revision-conflict",
                "regenerate-external-delete",
            }:
                await _HarnessState.coordinate_after_delta()
            if (
                _HarnessState.scenario_id == "regenerate-cancel-release-retry"
                and call_index == 1
            ):
                raise asyncio.CancelledError()

        async def chat_completion(self, messages):
            _HarnessState.increment("llm_calls")
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
            _HarnessState.increment("retrieval_calls")
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

        _HarnessState.increment("retrieval_calls")
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
        _HarnessState.increment("deep_review_calls")
        if _HarnessState.scenario_id == "deep-review-plan-failure":
            raise RuntimeError(CANARY)
        return ["synthetic-subquestion"]

    async def deep_execute(question, db=None):
        _HarnessState.increment("deep_review_calls")
        _HarnessState.increment("retrieval_calls")
        return {"question": question, "answer": "synthetic-subanswer", "citations": []}

    async def deep_synthesize(topic, sub_answers):
        _HarnessState.increment("deep_review_calls")
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

    connection_trace = {"count": 0}
    fault_trace = {
        "commit_attempts": 0,
        "injections": 0,
        "write_transactions": 0,
    }
    engine = create_engine(
        f"sqlite:///{db_path}",
        connect_args={"check_same_thread": False},
        poolclass=NullPool,
    )

    @event.listens_for(engine, "connect")
    def configure(dbapi_connection, connection_record):
        connection_trace["count"] += 1
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
            fault_trace["commit_attempts"] += 1
            if fail_final_commit:
                fault_trace["write_transactions"] += int(self.in_transaction())
                fault_trace["injections"] += 1
                raise RuntimeError(CANARY)
            return super().commit()

    normal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    finalizer = sessionmaker(
        autocommit=False,
        autoflush=False,
        bind=engine,
        class_=FailingSession if fail_final_commit else Session,
    )
    return engine, normal, finalizer, connection_trace, fault_trace


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
    _HarnessState.reset(scenario_id)
    fail_commit = scenario["failure"] == "commit"
    db_path = runtime_root / f"{scenario_id}.sqlite3"
    (
        engine,
        NormalSession,
        FinalizerSession,
        connection_trace,
        fault_trace,
    ) = _scenario_database(db_path, fail_commit)
    Base.metadata.create_all(bind=engine)
    database_module.SessionLocal = FinalizerSession

    original_regenerate = None
    conversation_id = None
    message_id = None
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
    with chat._ACTIVE_REGENERATIONS_LOCK:
        active_at_start = set(chat._ACTIVE_REGENERATIONS)

    request_count = 0
    peer_http_status = None
    retry_http_status = None
    retry_finished_count = 0
    retry_frames: list[dict[str, Any]] = []
    coordination_verified = False
    external_commit_verified = False
    worker_join_verified = True
    worker_exception_count = 0
    live_worker_count = 0
    coordination_wait_timeouts = 0
    active_409_reason_mismatch = 0
    secondary_dependency_call_count = 0
    cancel_release_failure = 0
    retry_failure = 0
    external_snapshot = None
    target_missing_before_release = False
    target_unchanged_after_cancel = True

    def regenerate_path() -> str:
        return (
            f"/api/chat/conversations/{conversation_id}/messages/"
            f"{message_id}/regenerate"
        )

    def post_regenerate(client):
        return client.post(regenerate_path(), json={"expected_revision": 0})

    coordinated_ids = {
        "regenerate-active-second-request",
        "regenerate-external-revision-conflict",
        "regenerate-external-delete",
    }
    response = None
    if scenario_id in coordinated_ids:
        worker_result: dict[str, Any] = {}

        def run_primary() -> None:
            try:
                worker_result["response"] = post_regenerate(TestClient(app))
            except BaseException as exc:  # noqa: BLE001 - worker 必须转成计数证据
                worker_result["exception_type"] = type(exc).__name__

        worker = threading.Thread(target=run_primary, daemon=True)
        worker.start()
        request_count += 1
        barrier_reached = _HarnessState.leader_after_delta.wait(timeout=5.0)
        if not barrier_reached:
            coordination_wait_timeouts += 1
        active_key = (conversation_id, message_id)
        with chat._ACTIVE_REGENERATIONS_LOCK:
            active_before_peer = active_key in chat._ACTIVE_REGENERATIONS
        try:
            if barrier_reached and scenario_id == "regenerate-active-second-request":
                before_peer = _HarnessState.snapshot()
                peer = post_regenerate(TestClient(app))
                request_count += 1
                peer_http_status = peer.status_code
                after_peer = _HarnessState.snapshot()
                secondary_dependency_call_count = int(
                    after_peer["llm_calls"] != before_peer["llm_calls"]
                    or after_peer["retrieval_calls"] != before_peer["retrieval_calls"]
                )
                active_409_reason_mismatch = int(
                    peer.status_code != 409
                    or peer.json().get("detail")
                    != "Message regeneration already active"
                )
            elif barrier_reached and scenario_id == "regenerate-external-revision-conflict":
                external_snapshot = (
                    "synthetic-external-answer",
                    [{"source": "p9_c9", "paper_id": 9}],
                    1,
                )
                with NormalSession() as external_db:
                    updated = (
                        external_db.query(Message)
                        .filter(Message.id == message_id, Message.revision == 0)
                        .update(
                            {
                                Message.content: external_snapshot[0],
                                Message.citations: external_snapshot[1],
                                Message.revision: external_snapshot[2],
                            },
                            synchronize_session=False,
                        )
                    )
                    external_db.commit()
                with NormalSession() as observe_db:
                    observed = observe_db.query(Message).filter(
                        Message.id == message_id
                    ).one_or_none()
                    external_commit_verified = bool(
                        updated == 1
                        and observed is not None
                        and (observed.content, observed.citations, observed.revision)
                        == external_snapshot
                    )
            elif barrier_reached and scenario_id == "regenerate-external-delete":
                peer = TestClient(app).delete(
                    f"/api/chat/conversations/{conversation_id}/messages/{message_id}"
                )
                request_count += 1
                peer_http_status = peer.status_code
                with NormalSession() as observe_db:
                    target_missing_before_release = (
                        observe_db.query(Message).filter(Message.id == message_id).first()
                        is None
                    )
                    observed_conv = observe_db.query(Conversation).filter(
                        Conversation.id == conversation_id
                    ).one()
                    actual_count = observe_db.query(Message).filter(
                        Message.conversation_id == conversation_id
                    ).count()
                    external_commit_verified = bool(
                        peer.status_code == 204
                        and target_missing_before_release
                        and observed_conv.message_count == actual_count == 1
                    )
            coordination_verified = bool(barrier_reached and active_before_peer)
        finally:
            _HarnessState.release_leader.set()
            worker.join(timeout=5.0)
            worker_join_verified = not worker.is_alive()
            live_worker_count = int(worker.is_alive())
        response = worker_result.get("response")
        worker_exception_count = int("exception_type" in worker_result)
    else:
        client = TestClient(app)
        if scenario["operation"] == "chat":
            response = client.post(
                "/api/chat",
                json={"message": "synthetic-question", "stream": True},
            )
            request_count = 1
        elif scenario["operation"] == "deep-review":
            response = client.post(
                "/api/chat/deep-review",
                json={"topic": "synthetic-question"},
            )
            request_count = 1
        elif scenario_id == "regenerate-cancel-release-retry":
            response = post_regenerate(client)
            request_count = 1
            with chat._ACTIVE_REGENERATIONS_LOCK:
                released_before_retry = (
                    (conversation_id, message_id) not in chat._ACTIVE_REGENERATIONS
                )
            cancel_release_failure = int(not released_before_retry)
            with NormalSession() as cancel_db:
                cancelled_target = cancel_db.query(Message).filter(
                    Message.id == message_id
                ).one_or_none()
                target_unchanged_after_cancel = bool(
                    cancelled_target is not None
                    and (
                        cancelled_target.content,
                        cancelled_target.citations,
                        cancelled_target.revision,
                    )
                    == original_regenerate
                )
            retry = post_regenerate(TestClient(app))
            request_count += 1
            retry_http_status = retry.status_code
            retry_frames = _parse_frames(retry.text)
            retry_finished_count = sum(
                frame.get("finished") is True for frame in retry_frames
            )
            retry_failure = int(
                retry.status_code != 200
                or retry_finished_count != 1
                or not retry_frames
                or retry_frames[-1].get("finished") is not True
            )
        else:
            response = post_regenerate(client)
            request_count = 1

    if response is None:
        response = types.SimpleNamespace(status_code=0, text="")
    frames = _parse_frames(response.text)
    terminal_indexes = [
        index
        for index, frame in enumerate(frames)
        if frame.get("finished") is True or "error" in frame
    ]
    terminals = [frames[index] for index in terminal_indexes]
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
        saved = None
        if original_regenerate is not None:
            saved = verify_db.query(Message).filter(Message.id == message_id).one_or_none()

    db_ok = (
        len(conversations) == scenario["expected_conversations"]
        and len(messages) == scenario["expected_messages"]
        and len(assistants) == scenario["expected_assistants"]
        and count_mismatches == 0
    )
    if scenario["expected_revision"] is not None:
        db_ok = db_ok and saved is not None and saved.revision == scenario["expected_revision"]
    actual_terminal = (
        "finished"
        if finished_count == 1 and len(terminals) == 1
        else "error"
        if finished_count == 0 and len(terminals) == 1 and "error" in terminals[0]
        else "none"
        if len(terminals) == 0
        else "invalid"
    )
    terminal_ok = actual_terminal == scenario["expected_terminal"]
    actual_error_code = (
        terminals[0].get("error_code")
        if len(terminals) == 1 and "error" in terminals[0]
        else None
    )
    error_code_ok = actual_error_code == scenario["expected_error_code"]
    state_snapshot = _HarnessState.snapshot()
    fake_ok = (
        state_snapshot["llm_calls"] == scenario["expected_llm_calls"]
        and state_snapshot["retrieval_calls"] == scenario["expected_retrieval_calls"]
        and state_snapshot["deep_review_calls"] == scenario["expected_deep_review_calls"]
    )
    fault_ok = (
        fault_trace == {
            "commit_attempts": 1,
            "injections": 1,
            "write_transactions": 1,
        }
        if fail_commit
        else fault_trace == {
            "commit_attempts": 0,
            "injections": 0,
            "write_transactions": 0,
        }
    )
    with chat._ACTIVE_REGENERATIONS_LOCK:
        active_after = set(chat._ACTIVE_REGENERATIONS)
    active_release_verified = not active_after
    active_leak = int(scenario["operation"] == "regenerate" and not active_release_verified)
    response_texts = [response.text]
    response_texts.extend(
        json.dumps(frame, ensure_ascii=False) for frame in retry_frames
    )
    response_clean = all(
        CANARY not in text_value and "Traceback" not in text_value
        for text_value in response_texts
    )
    terminal_last = not terminal_indexes or terminal_indexes[-1] == len(frames) - 1
    allowed_errors = {
        "AI 服务暂时不可用，请稍后重试",
        "深度综述规划失败，请稍后重试",
        "深度综述任务失败，请稍后重试",
    }
    error_payload_ok = all(
        set(frame) <= {"error", "error_code", "conversation_id"}
        and frame.get("error") in allowed_errors
        for frame in terminals
        if "error" in frame
    )
    success_payload_ok = True
    if scenario["expected_terminal"] == "finished":
        success_payload_ok = bool(
            terminals
            and assistants
            and terminals[0].get("content") == assistants[-1].content
        )

    target_state_verified = True
    if scenario_id == "regenerate-commit-failure":
        target_state_verified = bool(
            saved is not None
            and (saved.content, saved.citations, saved.revision) == original_regenerate
        )
    elif scenario_id == "regenerate-active-second-request":
        target_state_verified = bool(
            saved is not None
            and saved.revision == 1
            and terminals
            and saved.content == terminals[0].get("content")
        )
    elif scenario_id == "regenerate-external-revision-conflict":
        target_state_verified = bool(
            saved is not None
            and external_snapshot is not None
            and (saved.content, saved.citations, saved.revision) == external_snapshot
        )
    elif scenario_id == "regenerate-external-delete":
        target_state_verified = saved is None
    elif scenario_id == "regenerate-cancel-release-retry":
        retry_terminal = next(
            (frame for frame in retry_frames if frame.get("finished") is True),
            None,
        )
        target_state_verified = bool(
            target_unchanged_after_cancel
            and saved is not None
            and saved.revision == 1
            and retry_terminal
            and saved.content == retry_terminal.get("content")
        )

    peer_ok = peer_http_status == scenario["expected_peer_http_status"]
    retry_ok = retry_http_status == scenario["expected_retry_http_status"]
    request_ok = request_count == scenario["expected_request_count"]
    coordination_expected = scenario_id in coordinated_ids
    coordination_ok = (
        coordination_verified if coordination_expected else not coordination_verified
    )
    external_expected = scenario_id in {
        "regenerate-external-revision-conflict",
        "regenerate-external-delete",
    }
    external_ok = (
        external_commit_verified if external_expected else not external_commit_verified
    )
    passed = (
        db_ok
        and terminal_ok
        and error_code_ok
        and fake_ok
        and fault_ok
        and response.status_code == scenario["expected_http_status"]
        and peer_ok
        and retry_ok
        and request_ok
        and response_clean
        and terminal_last
        and error_payload_ok
        and success_payload_ok
        and connection_trace["count"] >= 2
        and active_leak == 0
        and not active_at_start
        and target_state_verified
        and coordination_ok
        and external_ok
        and worker_join_verified
        and worker_exception_count == 0
        and coordination_wait_timeouts == 0
        and state_snapshot["coordination_timeouts"] == 0
        and active_409_reason_mismatch == 0
        and secondary_dependency_call_count == 0
        and cancel_release_failure == 0
        and retry_failure == 0
    )

    violations = {
        "success_control_failure_count": int(
            scenario_id == "chat-success-control" and not passed
        ),
        "scenario_failure_count": int(not passed),
        "active_regeneration_leak_count": active_leak,
        "fake_call_contract_mismatch": int(not fake_ok),
        "finished_on_failure_count": (
            0 if scenario["expected_terminal"] == "finished" else finished_count
        ),
        "multiple_terminal_count": max(0, len(terminals) - 1),
        "unsanitized_error_count": int(not response_clean),
        "assistant_rows_on_failure": max(
            0, len(assistants) - scenario["expected_assistants"]
        ),
        "orphan_conversation_count": int(
            scenario["operation"] == "deep-review"
            and scenario_id != "chat-success-control"
            and len(conversations) != scenario["expected_conversations"]
        ),
        "message_count_mismatch": count_mismatches,
        "regenerate_mutation_count": int(
            scenario["operation"] == "regenerate" and not target_state_verified
        ),
        "rollback_failure_count": int(not db_ok),
        "request_contract_mismatch": int(not request_ok or not peer_ok or not retry_ok),
        "error_code_mismatch": int(not error_code_ok),
        "coordination_timeout_count": (
            coordination_wait_timeouts + state_snapshot["coordination_timeouts"]
        ),
        "worker_exception_count": worker_exception_count,
        "live_worker_count": live_worker_count,
        "unexpected_active_key_count": len(active_at_start),
        "active_409_reason_mismatch_count": active_409_reason_mismatch,
        "secondary_dependency_call_count": secondary_dependency_call_count,
        "external_mutation_commit_failure_count": int(
            external_expected and not external_commit_verified
        ),
        "external_state_overwrite_count": int(
            scenario_id == "regenerate-external-revision-conflict"
            and not target_state_verified
        ),
        "target_resurrection_count": int(
            scenario_id == "regenerate-external-delete"
            and (not target_missing_before_release or saved is not None)
        ),
        "cancel_release_failure_count": cancel_release_failure,
        "retry_failure_count": retry_failure,
        "terminal_order_violation_count": int(not terminal_last),
    }
    report = {
        "scenario_id": scenario_id,
        "operation": scenario["operation"],
        "failure": scenario["failure"],
        "terminal": actual_terminal,
        "error_code": actual_error_code,
        "passed": passed,
        "terminal_count": len(terminals),
        "finished_count": finished_count,
        "db_invariants_passed": db_ok,
        "fake_calls": (
            state_snapshot["llm_calls"]
            + state_snapshot["retrieval_calls"]
            + state_snapshot["deep_review_calls"]
        ),
        "fake_llm_calls": state_snapshot["llm_calls"],
        "fake_retrieval_calls": state_snapshot["retrieval_calls"],
        "fake_deep_review_calls": state_snapshot["deep_review_calls"],
        "http_status": response.status_code,
        "peer_http_status": peer_http_status,
        "retry_http_status": retry_http_status,
        "request_count": request_count,
        "retry_finished_count": retry_finished_count,
        "coordination_verified": coordination_verified,
        "external_commit_verified": external_commit_verified,
        "target_state_verified": target_state_verified,
        "active_release_verified": active_release_verified,
        "worker_join_verified": worker_join_verified,
    }
    for key in active_after:
        chat._release_regeneration(key)
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
        real_modules.extend(
            name
            for name in sys.modules
            if any(
                name == prefix or name.startswith(prefix + ".")
                for prefix in _FORBIDDEN_REAL_MODULE_PREFIXES
            )
        )
        real_modules = sorted(set(real_modules))
        aggregate["network_attempts"] = audit["network_attempts"]
        aggregate["subprocess_attempts"] = audit["subprocess_attempts"]
        aggregate["private_path_attempts"] = audit["private_path_attempts"]
        aggregate["real_service_module_count"] = len(real_modules)

        log_path = runtime_root / "logs" / "app.log"
        log_text = log_path.read_text(encoding="utf-8") if log_path.exists() else ""
        aggregate["log_canary_leak_count"] = int(
            any(
                token in log_text
                for token in (CANARY, "synthetic-question", "synthetic-answer", "Traceback")
            )
        )
        offline_proof = {
            "fake_llm_calls": sum(
                item["fake_llm_calls"] for item in scenario_reports
            ),
            "fake_retrieval_calls": sum(
                item["fake_retrieval_calls"] for item in scenario_reports
            ),
            "fake_deep_review_calls": sum(
                item["fake_deep_review_calls"] for item in scenario_reports
            ),
            "request_count": sum(item["request_count"] for item in scenario_reports),
            "peer_request_count": sum(
                item["peer_http_status"] is not None for item in scenario_reports
            ),
            "retry_request_count": sum(
                item["retry_http_status"] is not None for item in scenario_reports
            ),
            "external_commit_count": sum(
                item["external_commit_verified"] for item in scenario_reports
            ),
            "coordinated_scenario_count": sum(
                item["coordination_verified"] for item in scenario_reports
            ),
            "network_attempts": audit["network_attempts"],
            "subprocess_attempts": audit["subprocess_attempts"],
            "private_path_attempts": audit["private_path_attempts"],
            "real_service_module_count": len(real_modules),
        }
        implementation_paths = (
            PROJECT_ROOT / "backend" / "app" / "routers" / "chat.py",
            PROJECT_ROOT / "backend" / "app" / "database.py",
            PROJECT_ROOT / "backend" / "app" / "models.py",
            PROJECT_ROOT / "backend" / "app" / "schemas.py",
        )
        implementation_bytes = b"".join(
            path.relative_to(PROJECT_ROOT).as_posix().encode("utf-8")
            + b"\0"
            + path.read_bytes()
            + b"\0"
            for path in implementation_paths
        )
        report = {
            "report_schema": REPORT_SCHEMA,
            "benchmark_id": BENCHMARK_ID,
            "license": fixture["license"],
            "synthetic": True,
            "fixture_sha256": _sha256(fixture_bytes),
            "runner_sha256": _sha256(MODULE_PATH.read_bytes()),
            "implementation_sha256": _sha256(implementation_bytes),
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
