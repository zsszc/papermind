import os
import tempfile
from pathlib import Path
from typing import Any

import yaml


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

            # 生产包只携带配置模板，真实 API Key 由用户在应用数据目录配置。
            # 禁止从 resources 复制真实 config.yaml，避免密钥随安装包分发。
            example_config = project_root / "config.yaml.example"

            if not config_path.exists() and example_config.exists():
                import shutil
                shutil.copy(example_config, config_path)
        else:
            config_path = project_root / "config.yaml"

        if not config_path.exists():
            config_path = project_root / "config.yaml.example"
        self._config_path = config_path
        with open(config_path, "r", encoding="utf-8") as f:
            self._config = yaml.safe_load(f) or {}

        # 回滚/升级兼容（Batch 24 T4）：数据目录模式下，旧版配置缺失的后增键
        # 按公开模板递归补默认值——仅内存合并，不回写磁盘（不覆盖用户配置字节），
        # 用户已有的自定义项（含真实 Key）一律保留优先。
        if env_data_dir:
            self._merge_template_defaults(project_root)

    @staticmethod
    def _deep_fill_missing(target: dict, template: dict) -> None:
        """把 template 中 target 缺失的键递归补入（仅内存）；同名键以 target 为准。"""
        for key, tpl_val in template.items():
            if key not in target:
                target[key] = tpl_val
            elif isinstance(target.get(key), dict) and isinstance(tpl_val, dict):
                Config._deep_fill_missing(target[key], tpl_val)

    def _merge_template_defaults(self, project_root: Path) -> None:
        example_config = project_root / "config.yaml.example"
        if not isinstance(self._config, dict) or not example_config.exists():
            return
        try:
            with open(example_config, "r", encoding="utf-8") as f:
                template = yaml.safe_load(f) or {}
            if isinstance(template, dict):
                self._deep_fill_missing(self._config, template)
        except Exception:
            # 模板读取失败不阻断启动：缺失键维持 get() 的 default 兜底。
            # logger 延迟导入——logger.py 依赖本模块的 config，顶层导入会循环。
            try:
                from app.core.logger import logger
                logger.warning("[config] 模板默认补齐失败，按现有配置继续")
            except Exception:
                pass

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
        """将当前配置原子写入私有配置文件。"""
        if not self._config_path:
            return

        target = self._config_path
        if target.name.endswith(".example"):
            target = target.with_name(target.name[: -len(".example")])
        target.parent.mkdir(parents=True, exist_ok=True)

        fd, temp_name = tempfile.mkstemp(
            prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
        )
        temp_path = Path(temp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                yaml.dump(
                    self._config,
                    f,
                    allow_unicode=True,
                    sort_keys=False,
                    default_flow_style=False,
                )
                f.flush()
                os.fsync(f.fileno())
            temp_path.chmod(0o600)
            os.replace(temp_path, target)
            self._config_path = target
        except Exception:
            temp_path.unlink(missing_ok=True)
            raise

    @property
    def runtime_root(self) -> Path:
        """返回可变运行时文件的统一根目录。

        开发模式使用项目根；Electron 生产模式使用 PAPERMIND_DATA_DIR。
        """
        env_data_dir = os.environ.get("PAPERMIND_DATA_DIR")
        path = Path(env_data_dir) if env_data_dir else Path(__file__).resolve().parents[3]
        path.mkdir(parents=True, exist_ok=True)
        return path

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
