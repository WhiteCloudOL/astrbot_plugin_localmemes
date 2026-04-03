import json
import random
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger

from .models import DEFAULT_MEME_TYPES, PlaceHolder


class DataManager:
    def __init__(self, config: AstrBotConfig, data_dir: Path):
        self.config = config
        self.emoji_types = self._load_emoji_types()
        self.base_dir = data_dir / "memes"
        self._init_folders()

    def _init_folders(self):
        """根据 emoji_types 初始化文件夹"""
        if not self.base_dir.exists():
            self.base_dir.mkdir(parents=True, exist_ok=True)
            logger.info(f"[本地表情包] 创建基础文件夹: {self.base_dir}")

        for tag in self.emoji_types.keys():
            tag_dir = self.base_dir / tag
            if not tag_dir.exists():
                tag_dir.mkdir(parents=True, exist_ok=True)
                logger.info(f"[本地表情包] 创建表情文件夹: {tag_dir}")

    def get_random_meme_image(self, tag: str) -> str | None:
        """从指定标签文件夹中获取随机一张图片的路径"""
        tag_dir = self.base_dir / tag
        if not tag_dir.exists():
            return None

        valid_extensions = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
        try:
            files = [p for p in tag_dir.iterdir() if p.is_file() and p.suffix.lower() in valid_extensions]
            if not files:
                return None

            chosen_file = random.choice(files)
            return str(chosen_file)
        except Exception as e:
            logger.error(f"[本地表情包] 读取文件夹 {tag_dir} 失败: {e}")
            return None

    def _default_emoji_types(self) -> dict[str, str]:
        return dict(DEFAULT_MEME_TYPES)

    def _load_emoji_types(self) -> dict:
        origin_value: Any = self.config.get("emoji_types", "")

        def _log_loaded_types(loaded_types: dict, source: str) -> dict:
            keys = sorted(loaded_types.keys())
            logger.info(
                f"[本地表情包] 表情标签载入成功(source={source})：count={len(keys)} keys={keys}"
            )
            return loaded_types

        if isinstance(origin_value, dict):
            loaded = origin_value if origin_value else self._default_emoji_types()
            return _log_loaded_types(loaded, "dict_config")

        if isinstance(origin_value, str):
            stripped = origin_value.strip()
            if not stripped:
                return _log_loaded_types(self._default_emoji_types(), "default_empty_string")
            try:
                parsed_types = json.loads(stripped)
                if isinstance(parsed_types, dict):
                    loaded = parsed_types if parsed_types else self._default_emoji_types()
                    return _log_loaded_types(loaded, "json_string")
                logger.error("[本地表情包] <表情标签>配置不是 JSON 对象，将使用默认标签。")
                return _log_loaded_types(self._default_emoji_types(), "default_non_object_json")
            except Exception as e:
                logger.error(f"[本地表情包] 解析<表情标签>信息失败，将使用默认标签。请检查配置格式是否正确。错误: {e}")
                return _log_loaded_types(self._default_emoji_types(), "default_parse_error")

        logger.error(f"[本地表情包] <表情标签>配置类型不支持({type(origin_value).__name__})，将使用默认标签。")
        return _log_loaded_types(self._default_emoji_types(), "default_unsupported_type")

    def replace_placeholder(self, msg: str, group_id: str = "", user_id: str = "") -> str:
        """
        替换 <> 占位符 in PlaceHolder
        """
        if not msg:
            return msg

        ph = PlaceHolder(self.emoji_types, group_id, user_id)

        for k, v in ph.typedic.items():
            if k in msg:
                if k == "<表情标签>" and isinstance(v, dict):
                    # 将 <表情标签> 格式化为 <key>: value 的形式
                    formatted_lines = [f"<{tag_name}>: {tag_desc}" for tag_name, tag_desc in v.items()]
                    v_str = "\n".join(formatted_lines)
                    msg = msg.replace(k, v_str)
                elif isinstance(v, (dict, list)):
                    v_str = json.dumps(v, ensure_ascii=False, indent=2)
                    msg = msg.replace(k, v_str)
                else:
                    msg = msg.replace(k, str(v))
        return msg
