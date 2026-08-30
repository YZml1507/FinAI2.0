"""FinAI 凭证管理模块。

提供安全的凭证访问，支持：
- 环境变量读取
- 凭证缓存
- 凭证验证
- 凭证轮换提醒

安全原则：
- 凭证永远不要硬编码在代码中
- 凭证永远不要提交到版本控制
- 凭证访问需要审计日志
"""

from __future__ import annotations

import logging
import os
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

logger = logging.getLogger(__name__)

# 凭证文件路径
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

# 凭证轮换提醒天数
CREDENTIAL_ROTATION_REMINDER_DAYS = 90

# H10: 凭证缓存配置
import threading
_credentials_cache: Dict[str, str] = {}
_credentials_loaded_at: Optional[datetime] = None
_credentials_lock = threading.Lock()
CREDENTIAL_CACHE_TTL_SECONDS = 300  # H10: 缓存TTL为5分钟


def load_env_file(env_path: Optional[Path] = None) -> Dict[str, str]:
    """从 .env 文件加载环境变量。

    Args:
        env_path: .env 文件路径，默认为项目根目录

    Returns:
        加载的环境变量字典
    """
    path = env_path or ENV_FILE
    if not path.exists():
        logger.warning(f"环境变量文件不存在: {path}")
        return {}

    env_vars = {}
    try:
        with open(path, "r", encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#"):
                    continue
                if "=" in line:
                    key, _, value = line.partition("=")
                    key = key.strip()
                    value = value.strip()
                    if value.startswith('"') and value.endswith('"'):
                        value = value[1:-1]
                    elif value.startswith("'") and value.endswith("'"):
                        value = value[1:-1]
                    env_vars[key] = value
    except (OSError, UnicodeDecodeError) as e:
        logger.error(f"读取环境变量文件失败: {e}", exc_info=True)

    return env_vars


def get_credential(
    key: str,
    required: bool = True,
    default: Optional[str] = None,
    env_path: Optional[Path] = None,
) -> Optional[str]:
    """安全获取凭证值。

    优先级：环境变量 > .env 文件 > 默认值

    Args:
        key: 环境变量名
        required: 是否必需
        default: 默认值
        env_path: .env 文件路径

    Returns:
        凭证值或默认值
    """
    global _credentials_cache, _credentials_loaded_at

    # 1. 先从环境变量获取
    value = os.environ.get(key)
    if value:
        return value

    with _credentials_lock:
        # H10: 从缓存获取（检查TTL）
        now = datetime.now()
        if _credentials_loaded_at:
            cache_age = (now - _credentials_loaded_at).total_seconds()
            # H10: 如果缓存未过期，尝试从缓存获取
            if cache_age < CREDENTIAL_CACHE_TTL_SECONDS:
                if key in _credentials_cache:
                    return _credentials_cache[key]
            else:
                # H10: 缓存已过期，清空缓存
                _credentials_cache.clear()
                _credentials_loaded_at = None

        # 2. 从 .env 文件加载（只有在缓存未命中或已过期时才执行）
        env_vars = load_env_file(env_path)
        _credentials_cache.update(env_vars)
        _credentials_loaded_at = now

        # P1-7: 从缓存取值，确保与上面的原子操作一致
        value = env_vars.get(key) or default

    if required and not value:
        logger.error(f"必需的凭证未配置: {key}")
        raise ValueError(f"必需的凭证未配置: {key}. 请在 .env 文件或环境变量中设置。")

    return value


def get_feishu_credentials(agent_name: str) -> Dict[str, str]:
    """获取飞书 Agent 凭证。

    Args:
        agent_name: Agent 名称 (claudecode, hermes, mimo, glm47, doubao_seed_code, etc.)

    Returns:
        包含 app_id, app_secret, endpoint 的字典
    """
    prefix = f"FEISHU_{agent_name.upper()}"

    app_id = get_credential(f"{prefix}_APP_ID", required=True)
    app_secret = get_credential(f"{prefix}_APP_SECRET", required=True)
    endpoint = get_credential(f"{prefix}_ENDPOINT", required=False, default="")

    return {
        "app_id": app_id,
        "app_secret": app_secret,
        "endpoint": endpoint,
    }


def get_ai_api_key(provider: str) -> str:
    """获取 AI 服务 API Key。

    Args:
        provider: 服务提供商 (deepseek, mistral, cerebras, youdao_deepseek_v4_flash)

    Returns:
        API Key
    """
    key_map = {
        "deepseek": "DEEPSEEK_API_KEY",
        "mistral": "MISTRAL_API_KEY",
        "cerebras": "CEREBRAS_API_KEY",
        "youdao_deepseek_v4_flash": "YOUDAO_DEEPSEEK_V4_FLASH_API_KEY",
    }

    env_key = key_map.get(provider.lower())
    if not env_key:
        raise ValueError(f"不支持的 AI 服务提供商: {provider}")

    return get_credential(env_key, required=True)


def get_youdao_credentials() -> Dict[str, str]:
    """P0-3 fix: 获取有道智云凭证。

    ⚠️ 警告: 返回的字典包含明文凭证，禁止序列化到日志！
    """
    logger.warning(
        "P0-3 fix: get_youdao_credentials() 返回明文凭证。"
        "禁止将返回值序列化到日志或外部存储！"
    )
    return {
        "app_id": get_credential("YOUDAO_APP_ID", required=True),
        "app_secret": get_credential("YOUDAO_APP_SECRET", required=True),
        "api_key": get_credential("YOUDAO_DEEPSEEK_V4_FLASH_API_KEY", required=True),
        "base_url": get_credential(
            "YOUDAO_DEEPSEEK_V4_FLASH_BASE_URL",
            required=False,
            default="https://openapi.youdao.com/llmgateway/api/v1",
        ),
        "model": get_credential(
            "YOUDAO_DEEPSEEK_V4_FLASH_MODEL",
            required=False,
            default="deepseek-v4-flash",
        ),
    }


def validate_credentials(required_keys: Optional[List[str]] = None) -> Dict[str, Any]:
    """H11: 验证所有必需凭证是否已配置。

    Args:
        required_keys: 必需的凭证键列表（可选，默认使用内置列表）

    Returns:
        验证结果字典
    """
    results = {
        "valid": True,
        "configured": [],
        "missing": [],
        "warnings": [],
    }

    # H11: 支持自定义必需凭证列表
    if required_keys is None:
        required_keys = [
            "DEEPSEEK_API_KEY",
            "FEISHU_CLAUDECODE_APP_ID",
            "FEISHU_CLAUDECODE_APP_SECRET",
        ]

    for key in required_keys:
        try:
            value = get_credential(key, required=False)
            if value:
                results["configured"].append(key)
            else:
                results["missing"].append(key)
                results["valid"] = False
        except (ValueError, KeyError):
            results["missing"].append(key)
            results["valid"] = False

    # 检查可选凭证
    optional_keys = [
        "MISTRAL_API_KEY",
        "CEREBRAS_API_KEY",
        "YOUDAO_DEEPSEEK_V4_FLASH_API_KEY",
        "YOUDAO_APP_ID",
        "YOUDAO_APP_SECRET",
    ]

    for key in optional_keys:
        value = get_credential(key, required=False)
        if value:
            results["configured"].append(key)
        else:
            results["warnings"].append(f"可选凭证未配置: {key}")

    return results


def clear_credentials_cache():
    """清除凭证缓存。"""
    global _credentials_cache, _credentials_loaded_at
    with _credentials_lock:
        _credentials_cache.clear()
        _credentials_loaded_at = None
    logger.info("凭证缓存已清除")


def reset_credentials_cache():
    """重置凭证缓存（用于测试）。"""
    global _credentials_cache, _credentials_loaded_at
    with _credentials_lock:
        _credentials_cache = {}
        _credentials_loaded_at = None


class CredentialManager:
    """凭证管理器（单例模式）。"""

    _instance = None

    _class_lock = threading.Lock()

    def __new__(cls):
        if cls._instance is None:
            with cls._class_lock:
                if cls._instance is None:
                    cls._instance = super().__new__(cls)
                    cls._instance._initialized = False
        return cls._instance

    def __init__(self):
        if not self._initialized:
            self._initialized = True
            self._access_log: list = []

    def get(self, key: str, required: bool = True, default: Optional[str] = None) -> Optional[str]:
        """获取凭证并记录访问日志。"""
        self._log_access(key, "get")
        return get_credential(key, required=required, default=default)

    def get_feishu(self, agent_name: str) -> Dict[str, str]:
        """获取飞书凭证并记录访问日志。"""
        self._log_access(f"feishu_{agent_name}", "get_feishu")
        return get_feishu_credentials(agent_name)

    def get_ai_key(self, provider: str) -> str:
        """获取 AI API Key 并记录访问日志。"""
        self._log_access(f"ai_{provider}", "get_ai_key")
        return get_ai_api_key(provider)

    def validate(self) -> Dict[str, Any]:
        """验证凭证配置。"""
        return validate_credentials()

    def _log_access(self, key: str, action: str):
        """记录凭证访问日志。"""
        self._access_log.append({
            "timestamp": datetime.now().isoformat(),
            "key": key,
            "action": action,
        })
        # 只保留最近100条日志
        if len(self._access_log) > 100:
            self._access_log = self._access_log[-100:]

    def get_access_log(self) -> list:
        """获取访问日志。"""
        return self._access_log.copy()


# 全局凭证管理器实例
credential_manager = CredentialManager()
