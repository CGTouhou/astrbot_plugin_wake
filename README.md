# 叫醒小鲸鱼 (astrbot_plugin_wake)

让机器人在群里被 @ 之后**不用再 @** 就能继续说话；以及在关键词出现时，用一个小模型判断「这条消息是不是在找她」。

## 它做什么

| 场景 | 行为 |
|---|---|
| 被 @ | 直接回复（不走小模型，零额外成本） |
| 她刚说过话，还在「窗口」内 | 每条群消息都让**小模型看一眼**：是不是在跟她说话？是 → 回 |
| 窗口外，消息命中关键词 | 让**小模型判断**：是在叫她/聊她感兴趣的话题？是 → 回 |
| 其他 | 什么都不做，交给 AstrBot 自带的概率回复 |

**每次唤醒前还会过一道概率**（`reply_probability`），避免她话太密。

## 为什么这么设计

- **只写一个标志**：唤醒动作就是 `event.is_at_or_wake_command = True`，剩下的全交给 AstrBot 原生流程（人格、上下文、工具、技能自动注入）。所以人格永远是当前对话绑定的那个，**上下文天然按人格隔离**。
- **开窗靠 LLM 真正生成**：只在 `on_llm_response` 里开窗/续窗。插件自动转发、`/reset` 这类指令回复不会误开窗。
- **主动插话不自己实现**：用 AstrBot 自带的 `provider_ltm_settings.active_reply.possibility_reply`。
- **不碰 prompt / contexts / request_llm**：和任何人格、上下文类插件都不冲突。

## 安装

1. 把整个 `astrbot_plugin_wake` 目录放进 `AstrBot/data/plugins/`
2. 重启 AstrBot（或 WebUI 里重载插件）
3. 打开 WebUI → 插件 → 叫醒小鲸鱼 → 配置

依赖：**无**。只需要 AstrBot `>=4.16,<5` 和 Python 3.10+。

## 配置（WebUI 插件配置页）

### 基础

| 项 | 说明 | 默认 |
|---|---|---|
| `enabled` | 总开关 | `true` |
| `whitelist_groups` | 只在这些群生效；空 = 所有群 | `[]` |
| `keywords` | 手动关键词（**包含匹配**，不区分大小写） | `[]` |
| `self_name` | 她自己的名字，**可填多个**（空格/逗号/顿号/竖线分隔，如 `小鲸鱼 大肥鱼`），第一个是主名 | `小鲸鱼` |
| `auto_keywords` | 自动把她自己的每一个名字都当关键词 | `true` |
| `window_seconds` | 说了一句话后，多久内继续参与对话；`0` = 关闭窗口功能 | `120` |
| `reply_probability` | 小模型判定「该回」之后，实际回复的概率（0~1）；**1.0 = 一定回** | `0.8` |
| `fail_open` | 小模型超时/报错时，默认放行（true=回） | `true` |
| `debug_log` | 打印判断过程到日志 | `false` |

### 判断模型

| 项 | 说明 | 默认 |
|---|---|---|
| `llm_provider` | 判断用哪个模型（如 `deepseek_1/deepseek-flash`）；空 = 当前会话的模型 | 空 |
| `judge_timeout` | 判断超时（秒，3~120），超时按 `fail_open` 处理 | `15` |
| `history_count` | 给判断模型看多少条历史 | `8` |
| `prompt_keyword` | 关键词/窗口外用的判断提示词（留空=内置默认） | 内置全文 |
| `prompt_window` | 窗口内用的判断提示词（留空=内置默认） | 内置全文 |

### 防抖（等对方把话打完）

| 项 | 说明 | 默认 |
|---|---|---|
| `debounce_seconds` | 窗口内：判定后等几秒再真正唤醒 | `2.5` |
| `debounce_outside` | 窗口外：同上；`0` = 跟随 `debounce_seconds` | `0` |

### 图片

| 项 | 说明 | 默认 |
|---|---|---|
| `enable_image` | 是否让插件自己转述「图/文字分开发」的图片 | `true` |
| `image_caption_provider` | 转述用哪个视觉模型；空 = 跟随 AstrBot `provider_settings` | 空 |

> 图片转述的**提示词**跟随 AstrBot 的 `provider_settings.image_caption_prompt`，不单独配置。

## 命令（管理员）

```
/wake on|off        开关
/wake status        看状态（窗口剩余、关键词、白名单）
/wake sec 120       窗口时长（秒）
/wake p 0.8         回复概率
/wake kw add 鲸鱼   加关键词
/wake kw del 鲸鱼   删关键词
/wake kw list       看关键词
/wake test 鲸鱼在吗  测试一句话会命中什么
```

## 成本

- 被 @ 唤醒：**0** 额外成本
- 窗口内每条消息判断一次：约 1.5K token（deepseek-flash 约 ¥0.001/次）
- 关键词命中判断一次：同样约 1.5K token
- 平时没有关键词、不在窗口：**完全不调用**

## 常见问题

**Q：她怎么知道「图」？**
A：插件在历史里给图片留一个 `[图片]` 占位（不带内容，省 token），让判断模型知道「刚才有人发了图」。她真回复时，图的文字描述由 AstrBot 自带的群聊上下文注入。

**Q：为什么发完图要等几秒她才看得见？**
A：AstrBot 的图片转述是**同步**的（`group_chat_context` 里 `await` 视觉模型），一张图 3~8 秒。发完图马上叫她，图可能还没转述完。

**Q：能不发关键词、只靠小模型判断吗？**
A：可以，把 `keywords` 清空、`auto_keywords` 关掉。但那样每条群消息都会调一次小模型，成本会上去。

**Q：和 `group_chat_plus`、`persona_router` 冲突吗？**
A：不冲突。本插件不碰 `system_prompt` / `contexts` / `request_llm`。

**Q：`reply_probability` 设 1.0 是什么意思？**
A：小模型判定「该回」之后**一定回**。设 0.5 就是一半概率回。

**Q：窗口内她会不会抢别人的话？**
A：会。窗口内的每一条消息都会判断——这是设计如此（你叫了她一次，之后一段时间她都在参与）。

## 结构

```
main.py      钩子 + 主流程
config.py    配置读取与规整
state.py     窗口状态（多群独立）
judge.py     调模型判断
prompts.py   提示词
```

换提示词改 `prompts.py`；换判断逻辑改 `judge.py`；窗口策略改 `state.py`；不要动 `main.py` 的主链路。

## 与其它插件的关系

- 与 `astrbot_plugin_group_chat_plus`、`persona_router` 不冲突（不碰 prompt/contexts）
- 被 @ 时 `is_at_or_wake_command` 本来就是 true，本插件不重复处理
- AstrBot 自带概率回复在 `is_at_or_wake_command = True` 时会跳过，所以不会双回

## 更新日志

### v1.1.2

- **修复 `[MSG_ID:xxx]` 污染判断输入**：qq_tools 等插件会往 `event.message_str` 尾部追加 `[MSG_ID:123]`，之前会被原样记进历史、参与合并、再写回消息，小模型看到一串数字 ID 会分心。现在记历史、判空、判指令、比较、写回前统一剥掉

### v1.1.1

- **修复连发消息丢失**：唤醒时把合并后的完整一句写回 `message_str`。之前只标记了触发唤醒的那一条，被合并进来的第 2~N 条内容小模型判断时看得到、回复时却看不到（表现为「叫醒了她，但回复像漏听了一句」）
- 合并上限由 10 条提升到 `MAX_HISTORY`（deque 容量），与历史缓存对齐

### v1.1.0
- 新增：`self_name` 支持多个名字（空格/逗号/顿号/竖线分隔）
- 新增：图片消息在历史里留 `[图片]` 占位，让判断模型知道「有图」
- 新增：`history_count` 可配（给判断模型看多少条历史）
- 新增：两档防抖（窗口内 / 窗口外分开配）
- 新增：`judge_timeout` 可配
- 优化：图片转述的提示词跟随 AstrBot `provider_settings.image_caption_prompt`
- 修复：图片缓存长期不清理导致的内存占用

### v1.0.0
- 首个版本：关键词唤醒 + 窗口内判断 + @ 直达

## 发布前检查

- [ ] `metadata.yaml` 的 `repo` 改成你自己的仓库地址
- [ ] 版本号与 `main.py` 启动日志一致
- [ ] 确认 `.gitignore` 已排除 `__pycache__/`
