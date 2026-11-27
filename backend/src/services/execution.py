import json
import logging

from src.config.settings import CHECKPOINT_DIR
from src.domain.checkpoint import JsonFileCheckpointStore
from src.domain.project import analyze


_CHECKPOINT_STORE = None


def checkpoint_store():
    """回测断点存储的单例，目录由 CHECKPOINT_DIR 配置，进程间共享。"""
    global _CHECKPOINT_STORE
    if _CHECKPOINT_STORE is None:
        _CHECKPOINT_STORE = JsonFileCheckpointStore(CHECKPOINT_DIR)
    return _CHECKPOINT_STORE


def calculate(rows, settings):
    try:
        result = analyze(rows, settings, checkpoint_store())
        json.dumps(result, ensure_ascii=False, allow_nan=False)
    except ValueError as error:
        return None, str(error)
    except Exception:
        logging.getLogger(__name__).exception('计算运行失败')
        return None, '计算遇到内部错误，请检查台账后重试。'
    return result, None
