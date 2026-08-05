"""本地单用户部署不得把无认证服务发布到非回环接口。"""

from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[2]


def _read(relative_path: str) -> str:
    return (PROJECT_ROOT / relative_path).read_text(encoding="utf-8")


def test_direct_start_commands_bind_backend_to_loopback():
    assert "--host 0.0.0.0" not in _read("scripts/start-demo.sh")
    assert "--reload --host 0.0.0.0" not in _read("README.md")
    assert "--host 127.0.0.1" in _read("scripts/start-demo.sh")


def test_compose_publishes_user_facing_ports_on_loopback_only():
    compose = _read("docker-compose.yml")
    assert '"127.0.0.1:8000:8000"' in compose
    assert '"127.0.0.1:3001:3000"' in compose
    assert '      - "8000:8000"' not in compose
    assert '      - "3001:3000"' not in compose


def test_docker_run_documentation_binds_port_to_loopback():
    deploy_doc = _read("docs/DEPLOY.md")
    assert "-p 127.0.0.1:8000:8000" in deploy_doc
    assert "-p 8000:8000" not in deploy_doc
