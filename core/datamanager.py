import hashlib
import json
import os
import random
import tempfile
import threading
from pathlib import Path
from typing import Any

from astrbot.api import AstrBotConfig, logger

from .models import DEFAULT_MEME_TYPES, PlaceHolder


class DataManager:
    VALID_IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}

    def __init__(self, config: AstrBotConfig, data_dir: Path):
        self.config = config
        self.emoji_types = self._load_emoji_types()
        self.base_dir = data_dir / "memes"
        self.hash_file = data_dir / "memes_hash.json"
        # 保护哈希内存状态与落盘过程，避免并发写入造成竞态
        self._hash_lock = threading.RLock()
        self._init_folders()
        self._init_meme_hashes()

    def _calculate_file_md5(self, file_path: Path, chunk_size: int = 8192) -> str:
        digest = hashlib.md5()
        with open(file_path, "rb") as f:
            for chunk in iter(lambda: f.read(chunk_size), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _load_saved_hashes(self) -> dict[str, dict[str, Any]]:
        if not self.hash_file.exists():
            return {}

        try:
            with open(self.hash_file, encoding="utf-8") as f:
                loaded = json.load(f)
        except Exception as e:
            logger.error(f"[本地表情包] 读取哈希文件失败: {e}")
            return {}

        if not isinstance(loaded, dict):
            return {}

        # 仅保留结构合法的条目，避免后续处理时反复判断
        normalized: dict[str, dict[str, Any]] = {}
        for rel_path, item in loaded.items():
            if isinstance(rel_path, str) and isinstance(item, dict):
                if "hash" in item and "mtime" in item:
                    normalized[rel_path] = item
        return normalized

    def _iter_meme_image_files(self):
        if not self.base_dir.exists():
            return

        for tag_dir in self.base_dir.iterdir():
            if not tag_dir.is_dir():
                continue
            for p in tag_dir.iterdir():
                if p.is_file() and p.suffix.lower() in self.VALID_IMAGE_EXTENSIONS:
                    yield p

    def _build_file_hash_record(
        self,
        file_path: Path,
        saved_data: dict[str, dict[str, Any]],
    ) -> tuple[str, dict[str, Any], bool]:
        rel_path = str(file_path.relative_to(self.base_dir)).replace("\\", "/")
        mtime = file_path.stat().st_mtime

        cached = saved_data.get(rel_path)
        if isinstance(cached, dict) and cached.get("mtime") == mtime and "hash" in cached:
            return rel_path, cached, False

        md5_hash = self._calculate_file_md5(file_path)
        return rel_path, {"hash": md5_hash, "mtime": mtime}, True

    def _init_meme_hashes(self):
        self.meme_hashes: dict[str, dict[str, Any]] = {}
        saved_data = self._load_saved_hashes()
        has_changes = False

        for file_path in self._iter_meme_image_files() or []:
            try:
                rel_path, record, changed = self._build_file_hash_record(file_path, saved_data)
                self.meme_hashes[rel_path] = record
                if changed:
                    has_changes = True
            except Exception as e:
                logger.error(f"[本地表情包] 处理文件哈希失败: {file_path}, {e}")

        # 已删除文件也会触发重写，清理过期记录
        if len(self.meme_hashes) != len(saved_data):
            has_changes = True

        if has_changes:
            self._save_hashes()

    def _save_hashes(self):
        try:
            with self._hash_lock:
                self.hash_file.parent.mkdir(parents=True, exist_ok=True)

                # 先写临时文件，再原子替换，降低并发/异常导致的文件损坏概率
                with tempfile.NamedTemporaryFile(
                    "w",
                    encoding="utf-8",
                    dir=self.hash_file.parent,
                    prefix=f"{self.hash_file.stem}_",
                    suffix=".tmp",
                    delete=False,
                ) as tmp_file:
                    json.dump(self.meme_hashes, tmp_file, ensure_ascii=False, indent=2)
                    tmp_path = Path(tmp_file.name)

                os.replace(tmp_path, self.hash_file)
        except Exception as e:
            logger.error(f"[本地表情包] 保存哈希文件失败: {e}")

    def is_meme_exists(self, md5_hash: str) -> bool:
        if not hasattr(self, "meme_hashes"):
            return False
        with self._hash_lock:
            for item in self.meme_hashes.values():
                if isinstance(item, dict) and item.get("hash") == md5_hash:
                    return True
        return False

    def add_meme_hash(self, file_path: str, md5_hash: str):
        if not hasattr(self, "meme_hashes"):
            self.meme_hashes = {}

        try:
            p = Path(file_path)
            rel_path = str(p.relative_to(self.base_dir)).replace("\\", "/")
            stat = p.stat()
            with self._hash_lock:
                self.meme_hashes[rel_path] = {"hash": md5_hash, "mtime": stat.st_mtime}
                self._save_hashes()
        except Exception as e:
            logger.error(f"[本地表情包] 添加图片哈希记录失败: {e}")

    def remove_meme_hash(self, file_path: str):
        if not hasattr(self, "meme_hashes"):
            return

        try:
            p = Path(file_path)
            rel_path = str(p.relative_to(self.base_dir)).replace("\\", "/")
            with self._hash_lock:
                if rel_path in self.meme_hashes:
                    del self.meme_hashes[rel_path]
                    self._save_hashes()
        except Exception as e:
            logger.error(f"[本地表情包] 删除图片哈希记录失败: {e}")

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

    def delete_random_meme_image(self, tag: str) -> bool:
        """随机删除指定标签文件夹下的一张图片"""
        tag_dir = self.base_dir / tag
        if not tag_dir.exists():
            return False

        try:
            files = [
                p for p in tag_dir.iterdir() if p.is_file() and p.suffix.lower() in self.VALID_IMAGE_EXTENSIONS
            ]
            if not files:
                return False

            chosen_file = random.choice(files)
            self.remove_meme_hash(str(chosen_file))

            chosen_file.unlink()
            logger.info(f"[本地表情包] 已随机删除图片: {chosen_file}")
            return True
        except Exception as e:
            logger.error(f"[本地表情包] 删除文件夹 {tag_dir} 下的随机图片失败: {e}")
            return False

    def get_random_meme_image(self, tag: str) -> str | None:
        """从指定标签文件夹中获取随机一张图片的路径"""
        tag_dir = self.base_dir / tag
        if not tag_dir.exists():
            return None

        try:
            files = [
                p for p in tag_dir.iterdir() if p.is_file() and p.suffix.lower() in self.VALID_IMAGE_EXTENSIONS
            ]
            if not files:
                return None

            chosen_file = random.choice(files)
            return str(chosen_file)
        except Exception as e:
            logger.error(f"[本地表情包] 读取文件夹 {tag_dir} 失败: {e}")
            return None

    def get_total_memes_count(self) -> int:
        """获取所有分类下的表情包总数"""
        count = 0
        if not self.base_dir.exists():
            return 0
        try:
            for tag_dir in self.base_dir.iterdir():
                if tag_dir.is_dir():
                    count += sum(
                        1
                        for p in tag_dir.iterdir()
                        if p.is_file() and p.suffix.lower() in self.VALID_IMAGE_EXTENSIONS
                    )
        except Exception as e:
            logger.error(f"[本地表情包] 计算表情包总数失败: {e}")
        return count

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
                logger.error(
                    f"[本地表情包] 解析<表情标签>信息失败，将使用默认标签。请检查配置格式是否正确。错误: {e}"
                )
                return _log_loaded_types(self._default_emoji_types(), "default_parse_error")

        logger.error(
            f"[本地表情包] <表情标签>配置类型不支持({type(origin_value).__name__})，将使用默认标签。"
        )
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
