import json
import os
import random
from pathlib import Path

from astrbot.api import AstrBotConfig, logger

from .models import PlaceHolder, meme_types


class DataManager:
    def __init__(self,config: AstrBotConfig, data_dir: Path):
        self.config = config
        self.emoji_types = self._load_emoji_types()
        self.base_dir = data_dir / "emojis"
        self._init_folders()
        pass

    def _init_folders(self):
        """根据 emoji_types 初始化文件夹"""
        if not os.path.exists(self.base_dir):
            os.makedirs(self.base_dir, exist_ok=True)
            logger.info(f"[本地表情包] 创建基础文件夹: {self.base_dir}")

        for tag in self.emoji_types.keys():
            tag_dir = os.path.join(self.base_dir, tag)
            if not os.path.exists(tag_dir):
                os.makedirs(tag_dir, exist_ok=True)
                logger.info(f"[本地表情包] 创建表情文件夹: {tag_dir}")

    def get_random_meme_image(self, tag: str) -> str | None:
        """从指定标签文件夹中获取随机一张图片的路径"""
        tag_dir = os.path.join(self.base_dir, tag)
        if not os.path.exists(tag_dir):
            return None

        valid_extensions = (".png", ".jpg", ".jpeg", ".gif", ".webp")
        try:
            files = [f for f in os.listdir(tag_dir) if f.lower().endswith(valid_extensions)]
            if not files:
                return None

            chosen_file = random.choice(files)
            return os.path.join(tag_dir, chosen_file)
        except Exception as e:
            logger.error(f"[本地表情包] 读取文件夹 {tag_dir} 失败: {e}")
            return None

    def _load_emoji_types(self) -> dict:
        origin_text: str = self.config.get("emoji_types","") # Json原始
        try:
            parsed_types = json.loads(origin_text)
            if isinstance(parsed_types, dict):
                return parsed_types
            return meme_types.types
            # 校验
        except Exception as e:
            # 回退默认
            logger.error(f"[本地表情包] 解析<表情标签>信息失败，请检查代码格式是否正确。错误:{e}")
            return meme_types.types

    def replace_placeholder(self, msg: str, group_id: str = "", user_id: str = "") -> str:
        """
        替换<>占位符 in PlaceHolder
        """
        if not msg:
            return msg

        ph = PlaceHolder(self.emoji_types, group_id, user_id)
        # 确保使用实例的 emoji_types，防止 models.py 中未正确赋值
        ph.typedic["<表情标签>"] = self.emoji_types

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
