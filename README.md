<div align="center">

![count](https://count.getloli.com/@:astrbot_plugin_localmemes?name=astrbot_plugin_localmemes&theme=asoul&padding=7&offset=0&align=center&scale=1&pixelated=1&darkmode=auto)

# AstrBot Plugin LocalMemes
### 本地表情包插件 · v1.1.2

让 Bot 在聊天中更“有活力”：  
支持**关键词替换模式**与**AI 规划模式**自动发送本地表情包，  
支持**自主图片识别学习**，自动分类存入对应表情目录。

</div>

> 💌 **欢迎提交 Issue / PR！**  
> 如果你在使用中遇到问题、想到新功能、或希望优化文档与代码，欢迎在仓库发起 **Issue** 或 **Pull Request**，一起把插件做得更好。

---

## ✨ 功能

- 🎭 本地表情包随机发送（按分类）
- 🧠 双模式触发
  - **文本替换模式**：在系统提示词中注入标签规则
  - **AI规划模式**：由 LLM 直接判断发送标签
- 📚 表情学习（可选）
  - 识别消息中的图片
  - 调用图片 LLM 输出分类标签
  - 自动保存到 `data/plugin_data/.../memes/<标签>/`
  - 记录保存成功 / 失败日志，便于排查
- ⚙️ 高度可配置（概率、重试、Provider、Prompt、标签字典）

---

## 📦 安装与目录

### 1) 插件目录
将插件放置于：
`AstrBot/data/plugins/astrbot_plugin_localmemes`

### 2) 表情包数据目录
运行后，默认表情数据目录为：
`AstrBot/data/plugin_data/astrbot_plugin_localmemes/memes`

建议按“标签名”创建子文件夹，例如：

```text
memes/
├─ happy/
│  ├─ a.jpg
│  └─ b.png
├─ angry/
└─ surprised/
```

> 若你修改了 `emoji_types`（表情标签字典），建议同步整理目录结构，避免出现无效分类。

---

## 🚀 快速开始

1. 在插件配置中保留默认 `emoji_types`（或按需修改）。
2. 往 `memes/<标签>/` 下放入对应表情图。
3. 设置 `activate_prob`（推荐 0.3~0.7）。
4. 二选一：
   - 使用默认文本替换模式（`ai_judge.enable = false`）
   - 开启 AI 规划模式（`ai_judge.enable = true` 并配置 provider）
5. （可选）开启学习模式：`ai_learning.enable = true`，并配置图片识别 provider。

---

## ⚙️ 配置项说明（基于 `_conf_schema.json`）

> 下表为主要配置说明，字段名与配置文件保持一致。

### 顶层配置

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---:|---|
| `activate_prob` | float | `0.5` | 插件激活概率（不是“必发图”概率）。只有命中激活后，才会进入标签判定与发图流程。 |
| `emoji_replace_prompt` | text | 内置模板 | 文本替换模式使用的提示词模板。开启 AI 规划模式后通常可忽略。 |
| `emoji_types` | text(JSON) | 内置 22 类 | 表情标签字典（键=标签名，值=标签含义）。**目录名需与键名一致**。 |

### `ai_judge`（AI规划判断）

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---:|---|
| `ai_judge.enable` | bool | `false` | 开启后切换为 AI 规划模式，禁用关键词替换注入。 |
| `ai_judge.max_retry` | int | `3` | 调用 LLM 最大重试次数。 |
| `ai_judge.provider_id` | string | `""` | 指定规划模型 Provider；为空时使用当前会话默认 Provider。 |
| `ai_judge.prompt` | text | 内置模板 | 用于情绪标签判定的提示词。 |

### `ai_learning`（图片学习）

| 配置项 | 类型 | 默认值 | 说明 |
|---|---|---:|---|
| `ai_learning.enable` | bool | `false` | 是否启用图片学习。 |
| `ai_learning.prob` | float | `0.5` | 学习触发概率（当消息包含图片时生效）。 |
| `ai_learning.max_retry` | int | `3` | 图片识别调用最大重试次数。 |
| `ai_learning.provider_id` | string | `""` | 指定图片识别 Provider；为空时使用当前会话默认 Provider。 |
| `ai_learning.prompt` | text | 内置模板 | 图片情绪标签识别提示词。 |

---

## 🧩 `emoji_types` 配置示例

> 必须是标准 JSON 字符串，键名会作为分类目录名。

```json
{
  "happy": "表达强烈开心、狂喜、庆祝等",
  "angry": "表达愤怒、控诉、情绪爆发",
  "surprised": "表达震惊、意外、不可思议",
  "speechless": "表达无语、语塞、无奈"
}
```

---

## 📝 日志行为说明

启用学习模式后，插件会输出以下关键日志：

- 图片识别调用成功/失败
- LLM 分类结果为空或未知分类
- 单张图片保存成功/失败
- 本轮学习汇总（分类、成功数量、失败数量）

建议在调试阶段保持日志可见，以便快速定位：
- Provider 不可用
- 标签不匹配
- 图片链接不可访问等问题

---

## ❓常见问题（FAQ）

### 1. 为什么文本后面跟随了一个表情标签\<happy>？
- 请检查是否安装了其他调用`on_decorating_result`，本插件因为使用了结果装饰器，如果其他插件使用了高于`5`的优先级，那么其他插件将会优先生效。

### 2. 为什么触发了但没有发送表情？
- `activate_prob` 未命中；
- 判定出的标签不在 `emoji_types` 中；
- 对应标签目录下没有可用图片。

### 3. 开了学习模式但没学到图片？
- `ai_learning.enable` 未开启或 `ai_learning.prob` 未命中；
- 图片 LLM 返回 `None` / 空字符串 / 未知标签；
- 图片链接不可下载或本地文件路径无效。

### 4. AI 规划模式和文本替换模式如何选？
- 追求稳定与简单：文本替换模式；
- 追求上下文理解与灵活性：AI 规划模式（推荐配合稳定 provider）。

### 5. 为什么始终无法触发插件？
- 请检查是否开启`流式输出`，如果你开启了流式输出，插件函数将会无法调用！

## 🩷 问题反馈与建议
| 方式 | 联系 |
| :--: | :--: |
| Github Issue | [跳转](https://github.com/WhiteCloudOL/astrbot_plugin_localmemes/issues) | 
|QQ群 [637174573](https://qm.qq.com/q/3f2bdkDsyW) | ![](https://docs.meowyun.cn/assets/yungroup.Jsn95Q4J.webp) |

## ♾️支持
[Astrbot帮助文档](https://astrbot.app)  
[清蒸云鸭文档](https://docs.meowyun.cn/index.html)

## 📄 License

本项目采用 `AGPL3.0` 协议。
