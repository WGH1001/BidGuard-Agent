# -*- coding: utf-8 -*-
"""阶段性最小桩：仅提供 get_auth_secret（供 backend/utils/crypto.py 派生 API Key 主密钥）。

摘自 Contract-Review-Agent backend/auth.py（T23/T42/T53 的密钥守卫逻辑，逻辑零改动，
仅开发默认密钥字面量改为本项目名）。完整鉴权层（HMAC 令牌签发 / bcrypt 密码校验 /
FastAPI 依赖注入 / 审计）待本项目 backend/models.py 表结构建立后按任务卡接入，
届时以 Contract-Review-Agent backend/auth.py 为蓝本扩展本文件。
"""
import logging
import os
import warnings

logger = logging.getLogger(__name__)

_DEV_DEFAULT_SECRET = 'dev-secret-bidguard-agent'
_SECRET = os.environ.get('AUTH_SECRET', _DEV_DEFAULT_SECRET)


def _check_startup_secret_warning() -> None:
    """模块加载时检查 AUTH_SECRET 环境变量，缺失时打印强安全警告。"""
    env_sec = os.environ.get('AUTH_SECRET', '').strip()
    if not env_sec:
        msg = (
            "[SECURITY WARNING] 未配置环境变量 AUTH_SECRET，当前使用开发默认密钥！"
            "生产环境部署请务必配置独立的 AUTH_SECRET（开发默认密钥仅允许在 DEBUG=1 时启用）。"
        )
        logger.warning(msg)
        warnings.warn(msg, UserWarning, stacklevel=2)


_check_startup_secret_warning()


def get_auth_secret() -> str:
    """获取当前生效的 HMAC 签名密钥。

    规则：
        1. 若环境变量 AUTH_SECRET 非空，直接采用；
        2. 若模块级 _SECRET 被外部显式覆盖为非默认值，采用 _SECRET；
        3. 若未设置 AUTH_SECRET：检查环境变量 DEBUG（缺省 '1' 兼容本地与单测），
           若 DEBUG 显式设为 '0'/'false'/'no'/'off' 则抛出 RuntimeError 拒绝启动/签发；
           否则返回开发默认密钥 _DEV_DEFAULT_SECRET。
    """
    env_sec = os.environ.get('AUTH_SECRET', '').strip()
    if env_sec:
        # T53 强化：显式把 AUTH_SECRET 设为开发默认值同样视为未配置——
        # 生产态（DEBUG=0）下阻断，防止"照抄文档默认值"的伪安全配置。
        if env_sec == _DEV_DEFAULT_SECRET:
            debug_flag0 = os.environ.get('DEBUG', '1').strip().lower()
            if debug_flag0 in ('0', 'false', 'no', 'off'):
                raise RuntimeError(
                    "AUTH_SECRET 被显式设置为开发默认值，且 DEBUG 已关闭——"
                    "生产环境严禁使用开发默认密钥！"
                )
        return env_sec
    if _SECRET and _SECRET != _DEV_DEFAULT_SECRET:
        return _SECRET
    debug_flag = os.environ.get('DEBUG', '1').strip().lower()
    if debug_flag in ('0', 'false', 'no', 'off'):
        raise RuntimeError(
            "未配置环境变量 AUTH_SECRET，且 DEBUG 已关闭（非 DEBUG=1 模式），"
            "生产环境严禁使用开发默认密钥！"
        )
    return _DEV_DEFAULT_SECRET
