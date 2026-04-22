import asyncio
import hashlib
import ipaddress
import os
import random
import re
import shutil
import socket
import tempfile
import uuid
from pathlib import Path
from typing import Any
from urllib.parse import urlparse

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Image, Plain
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools
from astrbot.core.utils.io import download_image_by_url

from .core.datamanager import DataManager


class LocalMemesPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir(self.name)
        self.data_manager = DataManager(config=config, data_dir=self.data_dir)
        # 初始化配置
        self.config = config
        self.ai_judge_config: dict[str, Any] = self.config.get("ai_judge", {})
        self.ai_learning_config: dict[str, Any] = self.config.get("ai_learning", {})
        self.divide_group_config: dict[str, Any] = self.config.get("divide_group", {})
        self.enable_ai_judge = self.ai_judge_config.get("enable", False)
        self.enable_ai_learning = self.ai_learning_config.get("enable", False)

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""

    async def is_activated(self, scene: str = "activate") -> bool:
        """按场景判断是否触发概率。

        - activate: 表情包调用激活概率（activate_prob）
        - learning: 表情包学习概率（ai_learning.prob）
        """
        if scene == "learning":
            prob = self.ai_learning_config.get("prob", 0.5)
            prob_name = "learning_prob"
        else:
            prob = self.config.get("activate_prob", 0.5)
            prob_name = "activate_prob"

        try:
            prob = float(prob)
        except (TypeError, ValueError):
            logger.warning(f"[本地表情包] {prob_name} 配置无效，已回退到 0.5")
            prob = 0.5

        if prob <= 0:
            return False
        if prob >= 1:
            return True
        return random.random() < prob

    def _is_session_allowed(
        self,
        group_id: str | int | None,
        user_id: str | int | None,
        scene: str,
    ) -> bool:
        """按会话黑白名单规则判断是否允许触发插件逻辑。

        - 群聊（group_id 非空）：使用群组控制配置
        - 私聊（group_id 为空）：使用用户控制配置
        """
        is_group_chat = bool(group_id)
        target_type = "group" if is_group_chat else "user"
        target_id = str(group_id if is_group_chat else user_id).strip()

        if not target_id:
            # 无有效会话标识时默认放行
            return True

        if is_group_chat:
            block_method = str(
                self.divide_group_config.get(
                    "group_block_method",
                    self.divide_group_config.get("block_method", "黑名单"),
                )
            ).strip()
            control_list_raw = self.divide_group_config.get(
                "group_control_list",
                self.divide_group_config.get("control_list", []),
            ) or []
        else:
            block_method = str(
                self.divide_group_config.get(
                    "user_block_method",
                    self.divide_group_config.get("block_method", "黑名单"),
                )
            ).strip()
            control_list_raw = self.divide_group_config.get("user_control_list", []) or []

        control_list = {str(item).strip() for item in control_list_raw if str(item).strip()}

        if block_method == "白名单":
            allowed = target_id in control_list
            if not allowed:
                logger.info(
                    f"[本地表情包] 会话控制(白名单)拦截 {scene}：{target_type}_id={target_id}"
                )
            return allowed

        # 默认黑名单模式
        blocked = target_id in control_list
        if blocked:
            logger.info(
                f"[本地表情包] 会话控制(黑名单)拦截 {scene}：{target_type}_id={target_id}"
            )
            return False
        return True

    def _normalize_llm_output(self, text: str) -> str:
        """标准化 LLM 输出文本，尽量剥离包裹层（代码块、引号、尖括号）。"""
        normalized = (text or "").strip()
        if not normalized:
            return ""

        # 兼容 ```text ... ``` / ``` ... ```
        code_block_match = re.match(r"^```[a-zA-Z0-9_-]*\s*\n?(.*?)\n?```$", normalized, re.S)
        if code_block_match:
            normalized = code_block_match.group(1).strip()

        # 反复剥离成对包裹符号
        wrappers = [('"', '"'), ("'", "'"), ("“", "”"), ("<", ">"), ("`", "`")]
        changed = True
        while changed and normalized:
            changed = False
            for left, right in wrappers:
                if normalized.startswith(left) and normalized.endswith(right) and len(normalized) >= 2:
                    normalized = normalized[1:-1].strip()
                    changed = True

        return normalized

    def _extract_candidate_tokens(self, normalized_text: str) -> list[str]:
        """从标准化后的输出中提取候选 token。"""
        if not normalized_text:
            return []

        # 允许常见分隔符；严格模式下要求最终只能得到 1 个 token
        parts = re.split(r"[\s,，、;；|/]+", normalized_text)
        tokens = []
        for item in parts:
            token = item.strip().strip("\"'“”<>`")
            if token:
                tokens.append(token)
        return tokens

    def _parse_single_tag_result(
        self,
        raw_text: str,
        scene: str,
        allow_none: bool = True,
    ) -> tuple[str | None, str]:
        """严格解析 LLM 返回的单标签结果。"""
        normalized = self._normalize_llm_output(raw_text)
        if not normalized:
            return None, "empty_output"

        tokens = self._extract_candidate_tokens(normalized)
        if not tokens:
            return None, "no_token"

        # 去重保序
        unique_tokens: list[str] = []
        for t in tokens:
            if t not in unique_tokens:
                unique_tokens.append(t)

        if len(unique_tokens) != 1:
            return None, f"ambiguous_tokens={unique_tokens}"

        token = unique_tokens[0]
        if allow_none and token.lower() == "none":
            return None, "llm_returned_none"

        if token not in self.data_manager.emoji_types:
            return None, f"unknown_tag={token}"

        return token, "ok"

    async def call_llm_action(self, umo: str, prompt: str) -> str:
        """调用LLM处理, type: learn, answer, image, planner"""
        provider_id = self.ai_judge_config.get("provider_id", "")
        max_retry = int(self.ai_judge_config.get("max_retry", 3))

        if max_retry < 1:
            max_retry = 1

        if not provider_id:
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)  # type: ignore

        last_err: Exception | None = None

        for attempt in range(1, max_retry + 1):
            try:
                logger.info(
                    f"[本地表情包] 正在唤起LLM API: {provider_id} (重试次数 {attempt}/{max_retry})"
                )
                llm_resp: LLMResponse = await self.context.llm_generate(  # type: ignore
                    chat_provider_id=provider_id,
                    prompt=prompt,
                )

                llm_res = (llm_resp.completion_text or "").strip()
                if not llm_res:
                    raise ValueError("LLM returned empty completion_text")

                logger.info(f"[本地表情包] LLM {provider_id} 响应成功: {llm_res}")
                return llm_res

            except Exception as e:
                last_err = e
                logger.error(
                    f"[本地表情包] 调用LLM API: {provider_id} 失败 (attempt {attempt}/{max_retry})，原因：{e}"
                )
                if attempt < max_retry:
                    # 退避：1,2,4,8... 秒，上限 8 秒 + 随机抖动
                    base = min(2 ** (attempt - 1), 8)
                    delay = base + random.uniform(0, 0.5)
                    await asyncio.sleep(delay)

        logger.error(f"[本地表情包] 调用LLM API: {provider_id} 重试次数已用尽，最后错误：{last_err}")
        return ""

    async def call_image_llm_action(self, umo: str, image_urls: list[str], prompt: str) -> str:
        """调用图片识别API理解图片"""
        provider_id = self.ai_learning_config.get("provider_id", "")
        max_retry = int(self.ai_learning_config.get("max_retry", 3))
        if max_retry < 1:
            max_retry = 1

        if not provider_id:
            provider_id = await self.context.get_current_chat_provider_id(umo=umo)  # type: ignore

        img_provider = self.context.get_provider_by_id(provider_id)  # type: ignore
        if not img_provider:
            logger.error(f"[本地表情包] 未找到ID为 {provider_id} 的LLM提供商，请在配置中修改图片识别模型")
            return ""

        last_err: Exception | None = None

        for attempt in range(1, max_retry + 1):
            try:
                logger.info(
                    f"[本地表情包] 正在唤起图片LLM API: {provider_id} (重试次数 {attempt}/{max_retry})"
                )
                llm_resp: LLMResponse = await self.context.llm_generate(  # type: ignore
                    chat_provider_id=provider_id,
                    image_urls=image_urls,
                    prompt=f"{prompt}",
                )

                llm_res = (llm_resp.completion_text or "").strip()
                if not llm_res:
                    raise ValueError("Image LLM returned empty completion_text")

                logger.info(f"[本地表情包] 图片LLM {provider_id} 响应成功: {llm_res}")
                return llm_res

            except Exception as e:
                last_err = e
                logger.error(
                    f"[本地表情包] 调用图片LLM API: {provider_id} 失败 (attempt {attempt}/{max_retry})，原因：{e}"
                )
                if attempt < max_retry:
                    # 退避：1,2,4,8... 秒，上限 8 秒 + 随机抖动
                    base = min(2 ** (attempt - 1), 8)
                    delay = base + random.uniform(0, 0.5)
                    await asyncio.sleep(delay)

        logger.error(f"[本地表情包] 调用图片LLM API: {provider_id} 重试次数已用尽，最后错误：{last_err}")
        return ""

    def _extract_image_urls_from_message(self, event: AstrMessageEvent) -> list[str]:
        """遍历消息中的图片消息段，提取其中的图片 URL/文件引用。"""
        image_urls: list[str] = []
        message_chain = getattr(event.message_obj, "message", []) or []

        for comp in message_chain:
            if not isinstance(comp, Image):
                continue

            image_ref = (getattr(comp, "url", "") or getattr(comp, "file", "") or "").strip()
            if not image_ref:
                logger.debug("[本地表情包] 跳过无有效链接的图片消息段")
                continue

            image_urls.append(image_ref)

        return image_urls

    def _is_temp_file_path(self, path: str) -> bool:
        """判断路径是否为系统临时目录下的真实文件。"""
        try:
            resolved = Path(path).resolve()
            temp_root = Path(tempfile.gettempdir()).resolve()
            return resolved.is_file() and temp_root in resolved.parents
        except Exception:
            return False

    def _is_private_or_local_ip(self, ip_str: str) -> bool:
        try:
            ip_obj = ipaddress.ip_address(ip_str)
        except ValueError:
            return True

        return (
            ip_obj.is_private
            or ip_obj.is_loopback
            or ip_obj.is_link_local
            or ip_obj.is_multicast
            or ip_obj.is_reserved
            or ip_obj.is_unspecified
        )

    def _is_url_host_allowed_by_whitelist(self, host: str) -> bool:
        """域名白名单校验：为空则放行；非空时支持整段/后缀/域名层级标签匹配。"""
        whitelist = self.ai_learning_config.get(
            "download_domain_whitelist",
            self.config.get("download_domain_whitelist", []),
        ) or []

        if isinstance(whitelist, str):
            whitelist = [item.strip() for item in whitelist.split(",") if item.strip()]

        normalized_host = host.lower().strip(".")
        if not normalized_host:
            return False

        host_labels = [label for label in normalized_host.split(".") if label]
        normalized_allow = {
            str(item).lower().strip(".").lstrip("*.").strip()
            for item in whitelist
            if str(item).strip()
        }

        if not normalized_allow:
            return True

        for allowed in normalized_allow:
            # 整段域名匹配
            if normalized_host == allowed:
                return True

            # 后缀匹配（支持二级/三级/... 子域名）
            if normalized_host.endswith(f".{allowed}"):
                return True

        return False

    def _is_safe_remote_image_url(self, image_url: str) -> bool:
        """远程图片 URL 安全校验：协议、白名单、内网地址拦截。"""
        try:
            parsed = urlparse(image_url)
        except Exception:
            logger.warning(f"[本地表情包] URL 解析失败，已拒绝: {image_url!r}")
            return False

        if parsed.scheme not in {"http", "https"}:
            logger.warning(f"[本地表情包] 非 HTTP/HTTPS 图片引用已拒绝: {image_url!r}")
            return False

        if parsed.username or parsed.password:
            logger.warning(f"[本地表情包] 含鉴权信息的 URL 已拒绝: {image_url!r}")
            return False

        hostname = parsed.hostname
        if not hostname:
            logger.warning(f"[本地表情包] URL 缺少主机名，已拒绝: {image_url!r}")
            return False

        if not self._is_url_host_allowed_by_whitelist(hostname):
            logger.warning(f"[本地表情包] 域名不在白名单中，已拒绝: host={hostname}")
            return False

        try:
            addr_infos = socket.getaddrinfo(
                hostname,
                parsed.port or (443 if parsed.scheme == "https" else 80),
                proto=socket.IPPROTO_TCP,
            )
        except socket.gaierror as e:
            logger.warning(f"[本地表情包] 域名解析失败，已拒绝: host={hostname}, err={e}")
            return False

        seen_ips: set[str] = set()
        for info in addr_infos:
            raw_ip = info[4][0]
            if not isinstance(raw_ip, str):
                continue
            ip = raw_ip

            if ip in seen_ips:
                continue
            seen_ips.add(ip)

            if self._is_private_or_local_ip(ip):
                logger.warning(
                    f"[本地表情包] 检测到内网/本地地址，已拒绝下载: host={hostname}, ip={ip}"
                )
                return False

        return True

    def _detect_image_extension(self, file_path: str) -> str | None:
        """通过文件头魔数识别图片格式，返回标准扩展名。"""
        try:
            with open(file_path, "rb") as f:
                header = f.read(64)
        except Exception as e:
            logger.warning(f"[本地表情包] 读取图片头失败: {file_path}, err={e}")
            return None

        if header.startswith(b"\x89PNG\r\n\x1a\n"):
            return ".png"
        if header.startswith(b"\xff\xd8\xff"):
            return ".jpg"
        if header.startswith((b"GIF87a", b"GIF89a")):
            return ".gif"
        if len(header) >= 12 and header.startswith(b"RIFF") and header[8:12] == b"WEBP":
            return ".webp"

        return None

    async def _calculate_image_hash(self, image_url: str) -> tuple[str | None, str | None]:
        """仅对安全远程 URL 下载并计算图片 MD5，返回 (md5_hash, 临时文件路径)。"""
        if not image_url.startswith(("http://", "https://")):
            logger.warning(f"[本地表情包] 已拒绝非远程图片引用: {image_url!r}")
            return None, None

        if not self._is_safe_remote_image_url(image_url):
            return None, None

        temp_path: str | None = None
        try:
            temp_fd, temp_path = tempfile.mkstemp(prefix="localmemes_", suffix=".img")
            os.close(temp_fd)

            await download_image_by_url(image_url, path=temp_path)

            ext = self._detect_image_extension(temp_path)
            if not ext:
                logger.warning(f"[本地表情包] 下载内容不是受支持图片格式，已拒绝: {image_url!r}")
                os.remove(temp_path)
                return None, None

            with open(temp_path, "rb") as f:
                md5_hash = hashlib.md5(f.read()).hexdigest()

            return md5_hash, temp_path
        except Exception as e:
            logger.error(f"[本地表情包] 计算图片哈希失败: {e}")
            if temp_path and os.path.exists(temp_path):
                os.remove(temp_path)
            return None, None

    async def _download_image_to_tag_dir(
        self, image_url: str, tag: str, source_path: str | None = None
    ) -> str | None:
        """将已校验的临时图片文件移动到对应表情标签目录。"""
        if tag not in self.data_manager.emoji_types:
            logger.warning(f"[本地表情包] 未知表情标签，无法保存图片: {tag}")
            return None

        if not source_path or not self._is_temp_file_path(source_path):
            logger.warning(f"[本地表情包] 非受信任临时文件，已拒绝保存: {image_url!r}")
            return None

        ext = self._detect_image_extension(source_path)
        if not ext:
            logger.warning(f"[本地表情包] 图片格式校验失败，已拒绝保存: {image_url!r}")
            if os.path.exists(source_path):
                os.remove(source_path)
            return None

        tag_dir = self.data_manager.base_dir / tag
        tag_dir.mkdir(parents=True, exist_ok=True)
        target_path = tag_dir / f"learned_{uuid.uuid4().hex}{ext}"

        try:
            shutil.move(source_path, str(target_path))
            logger.info(f"[本地表情包] 图片保存成功: {target_path}")

            with open(target_path, "rb") as f:
                self.data_manager.add_meme_hash(str(target_path), hashlib.md5(f.read()).hexdigest())

            return str(target_path)
        except Exception as e:
            logger.warning(f"[本地表情包] 保存图片失败: {image_url}，错误: {e}")
            if os.path.exists(source_path):
                os.remove(source_path)
            if target_path.exists():
                target_path.unlink(missing_ok=True)
            return None

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """当AI请求时，在提示词中添加表情包引用"""
        if not await self.is_activated("activate"):
            return

        if self.enable_ai_judge:
            return

        logger.info(f"当前模式：{'AI规划模式' if self.enable_ai_judge else '文本替换模式'}")

        message_obj = getattr(event, "message_obj", None)
        group_id = getattr(message_obj, "group_id", "")
        sender = getattr(message_obj, "sender", None)
        user_id = getattr(sender, "user_id", "")

        try:
            emoji_replace_prompt = self.config.get("emoji_replace_prompt", "")
            emoji_replace_prompt = self.data_manager.replace_placeholder(
                emoji_replace_prompt,
                group_id=str(group_id),
                user_id=str(user_id),
            )
            req.system_prompt += emoji_replace_prompt
            logger.info("[本地表情包] 当前使用文本替换模式，已在系统提示词添加表情包设定！")
        except Exception as e:
            logger.error(f"[本地表情包] 当前使用文本替换模式，系统提示词添加失败！错误：{e}")

    @filter.on_decorating_result(priority=5)
    async def on_decorating_result(self, event: AstrMessageEvent):
        if self.enable_ai_judge:
            return
        result = event.get_result()
        if not result or not result.chain:
            return

        tags = []
        for node in result.chain:
            if isinstance(node, Plain) and node.text:
                found = re.findall(r"<([^>]+)>", node.text)
                if found:
                    tags.extend(found)
                    node.text = re.sub(r"<([^>]+)>", "", node.text)
                    logger.info(f"[本地表情包] 解析到表情: {node.text}")

        if tags:
            # 存储标签到 event 中，以便 after_message_sent 使用
            setattr(event, "_detected_tags", tags)
        else:
            logger.info("[本地表情包] 文本替换模式未解析到任何标签")

    @filter.after_message_sent(priority=5)
    async def after_message_sent(self, event: AstrMessageEvent):
        """原有消息发出后，继续发送表情"""
        umo = event.unified_msg_origin
        group_id = event.get_group_id()
        user_id = event.get_sender_id()
        tags = []

        if not self._is_session_allowed(group_id, user_id, "reply"):
            return

        if self.enable_ai_judge and await self.is_activated("activate"):
            ai_judge_prompt = self.ai_judge_config.get("prompt", "")
            ai_judge_prompt = self.data_manager.replace_placeholder(
                ai_judge_prompt,
                group_id,
                user_id,
            )
            ai_judge_prompt += f"\n【输入文本】\n{event.message_str}"

            result = await self.call_llm_action(umo, ai_judge_prompt)
            tag, reason = self._parse_single_tag_result(result, scene="reply", allow_none=True)

            if tag is None:
                logger.info(
                    f"[本地表情包] 本次未发图（AI规划解析未命中）：reason={reason} raw={result!r}"
                )
                return

            logger.info(f"[本地表情包] AI规划解析成功：tag={tag}")
            tags = [tag]
        else:
            # 从 event 中获取之前在 on_decorating_result 中解析到的标签
            tags = getattr(event, "_detected_tags", [])
            if not tags:
                logger.info("[本地表情包] 本次未发图（文本替换模式未检测到标签）")
                return

        if not tags:
            logger.info("[本地表情包] 本次未发图（无可用标签）")
            return

        sent = False
        for tag in tags:
            if tag not in self.data_manager.emoji_types:
                logger.info(f"[本地表情包] 跳过未知标签：{tag}")
                continue

            logger.debug("[本地表情包] 正在尝试获取图片")
            img_path = self.data_manager.get_random_meme_image(tag)
            if img_path:
                await event.send(event.make_result().file_image(img_path))
                logger.info(f"[本地表情包] 获取表情包图片成功: {img_path}")
                sent = True
                break

            logger.info(f"[本地表情包] 标签 `{tag}` 没有可用图片，本次继续尝试下一标签")

        if not sent:
            logger.info(f"[本地表情包] 本次未发图（标签存在但无可用图片）：tags={tags}")

    @filter.event_message_type(filter.EventMessageType.ALL, priority=5)
    async def on_learning_memes(self, event: AstrMessageEvent):
        """从消息中识别图片表情并学习到相应分类中"""
        if not self.enable_ai_learning:
            return

        if not await self.is_activated("learning"):
            return

        umo = event.unified_msg_origin
        group_id = event.get_group_id()
        user_id = event.get_sender_id()

        if not self._is_session_allowed(group_id, user_id, "learning"):
            return

        max_memes = self.ai_learning_config.get("max_memes", 0)
        random_replace = self.ai_learning_config.get("random_replace", False)
        need_replace = False

        if max_memes > 0:
            current_memes_count = self.data_manager.get_total_memes_count()
            if current_memes_count >= max_memes:
                if random_replace:
                    logger.info(
                        f"[本地表情包] 检测到图片，当前表情数量 ({current_memes_count} / {max_memes})，已达到上限，已启用随机替换，继续学习。"
                    )
                    need_replace = True
                else:
                    logger.info(
                        f"[本地表情包] 检测到图片，当前表情数量 ({current_memes_count} / {max_memes})，已达到上限，停止学习。"
                    )
                    return
            else:
                logger.info(
                    f"[本地表情包] 检测到图片，当前表情数量 ({current_memes_count} / {max_memes})，开始学习"
                )

        image_urls = self._extract_image_urls_from_message(event)

        if not image_urls:
            return

        # 校验图片是否已存在
        new_images = []
        for url in image_urls:
            img_hash, source_path = await self._calculate_image_hash(url)
            if not img_hash or not source_path:
                logger.warning(f"[本地表情包] 图片预处理失败或被安全策略拦截，跳过学习: {url!r}")
                continue

            if self.data_manager.is_meme_exists(img_hash):
                logger.info(f"[本地表情包] 检测到图片已存在于库中 (MD5: {img_hash})，跳过学习。")
                if self._is_temp_file_path(source_path):
                    os.remove(source_path)
                continue

            new_images.append((url, source_path))

        if not new_images:
            logger.info("[本地表情包] 所有提取的图片均已存在于本地库中，本次取消AI识别及学习。")
            return

        image_urls_to_llm = [url for url, _ in new_images]

        if image_urls_to_llm:
            logger.info(
                f"[本地表情包] 已从消息中提取到 {len(image_urls_to_llm)} 个新图片引用，正在调用LLM识别"
            )
            learning_prompt = self.ai_learning_config.get("prompt", "")
            learning_prompt = self.data_manager.replace_placeholder(
                learning_prompt,
                group_id,
                user_id,
            )
            result = await self.call_image_llm_action(umo, image_urls_to_llm, learning_prompt)
            tag, reason = self._parse_single_tag_result(result, scene="learning", allow_none=True)

            if tag is None:
                logger.error(
                    f"[本地表情包] 表情包学习失败：分类解析未命中，reason={reason} raw={result!r}，已跳过保存"
                )
                # 清理未使用的临时文件
                for _, source_path in new_images:
                    if source_path and self._is_temp_file_path(source_path):
                        os.remove(source_path)
                return

            success_count = 0
            failed_count = 0

            for url, source_path in new_images:
                if need_replace:
                    self.data_manager.delete_random_meme_image(tag)

                saved_path = await self._download_image_to_tag_dir(url, tag, source_path)
                if saved_path:
                    success_count += 1
                    logger.info(f"[本地表情包] 已按分类 `{tag}` 保存图片: {saved_path}")
                else:
                    failed_count += 1
                    logger.error(f"[本地表情包] 按分类 `{tag}` 保存图片失败: {url}")

            logger.info(
                f"[本地表情包] 表情包学习完成：分类={tag}，成功={success_count}，失败={failed_count}"
            )

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        pass
