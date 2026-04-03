import asyncio
import os
import random
import re
import shutil
import uuid
from typing import Any

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
        self.data_manager = DataManager(config=config,data_dir = self.data_dir)
        #初始化配置
        self.config = config
        self.ai_judge_config: dict[str, Any] = self.config.get("ai_judge",{})
        self.ai_learning_config: dict[str, Any] = self.config.get("ai_learning",{})
        self.divide_group_config: dict[str, Any] = self.config.get("divide_group", {})
        self.enable_ai_judge = self.ai_judge_config.get("enable",False)
        self.enable_ai_learning = self.ai_learning_config.get("enable",False)

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
                logger.info(f"[本地表情包] 正在唤起LLM API: {provider_id} (重试次数 {attempt}/{max_retry})")
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

    async def _download_image_to_tag_dir(self, image_url: str, tag: str) -> str | None:
        """将指定图片下载或复制到对应表情标签目录。"""
        if tag not in self.data_manager.emoji_types:
            logger.warning(f"[本地表情包] 未知表情标签，无法保存图片: {tag}")
            return None

        tag_dir = os.path.join(self.data_manager.base_dir, tag)
        os.makedirs(tag_dir, exist_ok=True)

        ext = os.path.splitext(image_url.split("?")[0])[1].lower()
        if ext not in {".png", ".jpg", ".jpeg", ".gif", ".webp"}:
            ext = ".jpg"

        target_path = os.path.join(tag_dir, f"learned_{uuid.uuid4().hex}{ext}")

        try:
            if image_url.startswith(("http://", "https://")):
                await download_image_by_url(image_url, path=target_path)
            elif image_url.startswith("file:///"):
                shutil.copyfile(image_url[8:], target_path)
            elif os.path.exists(image_url):
                shutil.copyfile(image_url, target_path)
            else:
                logger.warning(f"[本地表情包] 不支持的图片引用，无法保存: {image_url}")
                return None

            logger.info(f"[本地表情包] 图片保存成功: {target_path}")
            return target_path
        except Exception as e:
            logger.warning(f"[本地表情包] 下载图片失败: {image_url}，错误: {e}")
            return None


    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """当AI请求时，在提示词中添加表情包引用"""
        if not await self.is_activated("activate"):
            return

        if self.enable_ai_judge:
            return

        logger.info(f"当前模式：{'AI规划模式' if self.enable_ai_judge else '文本替换模式'}")

        group_id = getattr(event.message_obj, "group_id", "")
        user_id = getattr(event.message_obj.sender, "user_id", "")

        try:
            emoji_replace_prompt = self.config.get("emoji_replace_prompt","")
            emoji_replace_prompt = self.data_manager.replace_placeholder(
                emoji_replace_prompt,
                group_id=str(group_id),
                user_id=str(user_id)
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

    @filter.after_message_sent()
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
                user_id
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

    @filter.event_message_type(filter.EventMessageType.ALL)
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

        image_urls = self._extract_image_urls_from_message(event)

        if image_urls:
            logger.info(
                f"[本地表情包] 已从消息中提取到 {len(image_urls)} 个图片引用，正在调用LLM识别"
            )
            learning_prompt = self.ai_learning_config.get("prompt","")
            learning_prompt = self.data_manager.replace_placeholder(
                learning_prompt,
                group_id,
                user_id
            )
            result = await self.call_image_llm_action(umo, image_urls, learning_prompt)
            tag, reason = self._parse_single_tag_result(result, scene="learning", allow_none=True)

            if tag is None:
                logger.error(
                    f"[本地表情包] 表情包学习失败：分类解析未命中，reason={reason} raw={result!r}，已跳过保存"
                )
                return

            success_count = 0
            failed_count = 0

            for image_url in image_urls:
                saved_path = await self._download_image_to_tag_dir(image_url, tag)
                if saved_path:
                    success_count += 1
                    logger.info(f"[本地表情包] 已按分类 `{tag}` 保存图片: {saved_path}")
                else:
                    failed_count += 1
                    logger.error(f"[本地表情包] 按分类 `{tag}` 保存图片失败: {image_url}")

            logger.info(
                f"[本地表情包] 表情包学习完成：分类={tag}，成功={success_count}，失败={failed_count}"
            )



    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
        pass
