import os
import yaml
from pathlib import Path
from typing import Any


class Config:
    _instance = None
    _config: dict = {}

    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
            cls._instance._config_path = None
            cls._instance._load()
        return cls._instance

    def _load(self):
        project_root = Path(__file__).resolve().parents[3]

        # Electron 生产包：从 resources 根目录读取配置
        env_data_dir = os.environ.get("PAPERMIND_DATA_DIR")
        if env_data_dir:
            data_dir = Path(env_data_dir)
            data_dir.mkdir(parents=True, exist_ok=True)
            config_path = data_dir / "config.yaml"

            # 生产包中优先使用 app resources 里的真实配置（含真实 API key）
            bundled_config = project_root / "config.yaml"
            example_config = project_root / "config.yaml.example"

            def _is_placeholder_config(path: Path) -> bool:
                """检查配置是否还是占位符模板。"""
                if not path.exists():
                    return True
                try:
                    text = path.read_text(encoding="utf-8")
                    if "sk-xxxx" in text or "your-" in text.lower():
                        return True
                    cfg = yaml.safe_load(text) or {}
                    key = cfg.get("llm", {}).get("api_key", "")
                    if not key or "xxxx" in str(key) or str(key).startswith("your-"):
                        return True
                except Exception:
                    return True
                return False

            if bundled_config.exists() and (
                not config_path.exists() or _is_placeholder_config(config_path)
            ):
                import shutil
                shutil.copy(bundled_config, config_path)
            elif not config_path.exists() and example_config.exists():
                import shutil
                shutil.copy(example_config, config_path)
        else:
            config_path = project_root / "config.yaml"

        if not config_path.exists():
            config_path = project_root / "config.yaml.example"
        self._config_path = config_path
        with open(config_path, "r", encoding="utf-8") as f:
            self._config = yaml.safe_load(f) or {}

    def get(self, key: str, default: Any = None) -> Any:
        keys = key.split(".")
        value = self._config
        for k in keys:
            if isinstance(value, dict) and k in value:
                value = value[k]
            else:
                return default
        return value

    @property
    def config_path(self) -> Path:
        return self._config_path

    def reload(self):
        """重新加载配置文件。"""
        self._load()

    def save(self):
        """将当前配置写回文件。"""
        if not self._config_path:
            return
        with open(self._config_path, "w", encoding="utf-8") as f:
            yaml.dump(self._config, f, allow_unicode=True, sort_keys=False, default_flow_style=False)

    @property
    def data_dir(self) -> Path:
        # Electron 生产包优先使用应用数据目录
        env_data_dir = os.environ.get("PAPERMIND_DATA_DIR")
        if env_data_dir:
            path = Path(env_data_dir)
            path.mkdir(parents=True, exist_ok=True)
            return path

        path = Path(self.get("app.data_dir", "./data"))
        if not path.is_absolute():
            project_root = Path(__file__).resolve().parents[3]
            path = project_root / path
        path.mkdir(parents=True, exist_ok=True)
        return path


config = Config()
