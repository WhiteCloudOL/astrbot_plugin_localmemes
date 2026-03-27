import asyncio
import random
import re
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.message_components import Plain
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star, StarTools

from .core.datamanager import DataManager


class LocalMemesPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.data_dir = StarTools.get_data_dir(self.name)
        self.data_manager = DataManager(config=config,data_dir = self.data_dir)
        #初始化配置
        self.config = config
        self.ai_judge_config: dict[str, Any] = self.config.get("ai_judge",{})
        self.enable_ai_judge = self.ai_judge_config.get("enable_ai_judge",False)

    async def initialize(self):
        """可选择实现异步的插件初始化方法，当实例化该插件类之后会自动调用该方法。"""

    async def is_activated(self):
        """判断是否激活表情包"""
        activate_prob: float = self.config.get("activate_prob", 0.5)
        if activate_prob <= 0:
            return False
        if activate_prob >= 1:
            return True
        return random.random() < activate_prob

    def format_judge_llm_result(self,text: str) -> str:
        """
        删除文本中的: < > " ' “ ”
        返回清理后的新文本
        """
        return re.sub(r'[<>"\'“”]', "", text)

    async def call_llm_action(self, umo: str, prompt: str) -> str:
        """调用LLM处理, type: learn, answer, image, planner"""
        provider_id = self.ai_judge_config.get("judge_provider_id", "")
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


    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """当AI请求时，在提示词中添加表情包引用"""
        if not await self.is_activated():
            return

        if self.enable_ai_judge:
            return

        logger.info(f"当前模式：{self.enable_ai_judge}")

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

    @filter.on_decorating_result()
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
                    logger.info(f"表情: {node.text}")

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

        if self.enable_ai_judge and await self.is_activated():
            ai_judge_prompt = self.ai_judge_config.get("judge_prompt", "")
            ai_judge_prompt = self.data_manager.replace_placeholder(
                ai_judge_prompt,
                group_id,
                user_id
            )
            ai_judge_prompt+=f"\n【输入文本】\n{event.message_str}"

            try:
                result = await self.call_llm_action(umo, ai_judge_prompt)
                tag = self.format_judge_llm_result(result)
                logger.info(f"[本地表情包] 解析LLM结果成功！:{tag}")
                tags = [tag]
            except Exception as e:
                logger.warning(f"[本地表情包] 解析LLM结果出错，错误: {e}")
                return
        else:
            # 从 event 中获取之前在 on_decorating_result 中解析到的标签
            tags = getattr(event, "_detected_tags", [])

        if not tags:
            return

        for tag in tags:
            if tag in self.data_manager.emoji_types:
                logger.info("[本地表情包] 正在尝试获取图片")
                img_path = self.data_manager.get_random_meme_image(tag)
                if img_path:
                    await event.send(event.make_result().file_image(img_path))
                    break
                else:
                    logger.warning("[本地表情包] 未获取到表情包图片")

    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
