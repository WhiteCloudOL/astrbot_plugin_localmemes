import random
from typing import Any

from astrbot.api import AstrBotConfig, logger
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse, ProviderRequest
from astrbot.api.star import Context, Star


class LocalMemesPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)

        #初始化配置
        self.config = config
        self.ai_judge_config: dict[str, Any] = self.config.get("ai_judge",{})
        self.enable_ai_judge = self.ai_judge_config.get("enable_ai_judge",False)
        self.ai_judge_prompt = self.ai_judge_config.get("judge_prompt", "")

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


    async def call_llm_action(self, umo: str, prompt: str) -> str:
        """调用LLM处理, type: learn, answer, image, planner"""
        provider_id = self.config.get("provider_id","")

        if not provider_id:
           provider_id = await self.context.get_current_chat_provider_id(umo=umo) # type: ignore

        try:
            logger.info(f"[MeowYun] 正在唤起LLM API: {provider_id}")
            llm_resp: LLMResponse = await self.context.llm_generate(  # type: ignore
                    chat_provider_id=provider_id,
                    prompt=prompt,
            )
            llm_res = llm_resp.completion_text
            logger.info(f"[MeowYun] LLM {provider_id} 响应成功: {llm_res}")
            return llm_res
        except Exception as e:
            logger.error(f"[MeowYun] 调用LLM API: {provider_id} 失败，原因：{e}")
            return ""

    @filter.on_llm_request()
    async def on_llm_request(self, event: AstrMessageEvent, req: ProviderRequest):
        """当AI请求时，在提示词中添加表情包引用"""
        # 如果为AI判断模式
        if not self.enable_ai_judge:
            try:
                emoji_prompt = """你需要在回复文本时，根据回答内容的情感倾向，从以下标签字典中选择最相关的一个情绪标签附加在文本末尾（格式为<标签名>）：
PYTHON
tag_dict = {
    "happy": "正面、愉快、赞赏、成功或庆祝的内容",
    "sad": "悲伤、遗憾、同情或负面情绪的内容",
    "angry": "愤怒、不满或强烈抗议的内容",
    "confused": "困惑、不确定或需要澄清的内容",
    "surprised": "惊讶、意外或超出预期的内容",
    "thinking": "思考、分析或需要深度推理的内容",
    "neutral": "客观事实陈述或无明显情绪的内容"
}
选择规则：
严格评估文本与标签描述的匹配度
每次回复最多添加一个标签（如无匹配则不加）
标签必须用尖括号包裹且放在所有文本的最后
禁止连续添加多个标签或重复添加
示例：
"这个问题解决得很漂亮！<happy>"
"很抱歉听到这个消息<sad>"
"根据数据统计，气温上升了2℃" (无标签)

特别要求：

不得解释标签系统
保持原始回答质量不变
标签添加比例应自然合理（非强制每句添加）
"""
                req.system_prompt += emoji_prompt
                logger.info("[本地表情包] 当前使用文本替换模式，已在系统提示词添加表情包设定！")
            except Exception as e:
                logger.error(f"[本地表情包] 当前使用文本替换模式，系统提示词添加失败！错误：{e}")

    @filter.after_message_sent()
    async def after_message_sent(self, event: AstrMessageEvent):
        """原有消息发出后，继续发送表情"""
        umo = event.unified_msg_origin
        if self.enable_ai_judge:
            result = await self.call_llm_action(umo, self.ai_judge_prompt)



    async def terminate(self):
        """可选择实现异步的插件销毁方法，当插件被卸载/停用时会调用。"""
