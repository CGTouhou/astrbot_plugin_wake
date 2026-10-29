# 叫醒小鲸鱼 (astrbot_plugin_wake)

让机器人在群里被 @ 之后**不用再 @** 就能继续说话；以及在关键词出现时，用一个小模型判断「这条消息是不是在找她」。
私聊也可以加一道防抖：对方连发三句，合成一句再回，不会回三次。

## 它做什么

| 场景 | 行为 |
|---|---|
| 群里被 @ | 直接回复（不走小模型，零额外成本） |
| 她刚说过话，还在「窗口」内 | 每条群消息都让**小模型看一眼**：是不是在跟她说话？是 → 回 |
| 窗口外，消息命中关键词 | 让**小模型判断**：是在叫她/聊她感兴趣的话题？是 → 回 |
| 私聊 | 不判断，只防抖：等对方把话打完，合并成一句 |
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
| `window_seconds` | 说了一句话后，多久内继续参与对话；`0` = 关闭窗口功能 | `60` |
| `reply_probability` | 小模型判定「该回」之后，实际回复的概率（0~1）；**1.0 = 一定回** | `1.0` |
| `fail_open` | 小模型超时/报错时，默认放行（true=回） | `true` |
| `debug_log` | 打印判断过程到日志 | `false` |

### 判断模型

| 项 | 说明 | 默认 |
|---|---|---|
| `llm_provider` | 判断用哪个模型（如 `deepseek_1/deepseek-flash`）；空 = 当前会话的模型 | 空 |
| `judge_timeout` | 判断超时（秒，3~120），超时按 `fail_open` 处理 | `15` |
| `history_count` | 给判断模型看多少条历史 | `10` |
| `prompt_keyword` | 关键词/窗口外用的判断提示词（留空=内置默认） | 内置全文 |
| `prompt_window` | 窗口内用的判断提示词（留空=内置默认） | 内置全文 |

### 防抖（等对方把话打完）

群里被叫到、或者私聊收到消息后，先等一小会儿再动作 —— 对方一个字一个字敲的时候，只判一次、只回一次。

| 项 | 说明 | 默认 |
|---|---|---|
| `group_debounce_enabled` | 群聊防抖总开关；关掉 = 收到即判断 | `true` |
| `debounce_seconds` | 群聊·已在窗口内时的防抖 | `1.5` |
| `debounce_outside` | 群聊·窗口外首次叫她时的防抖 | `2.5` |
| `debounce_max_wait` | 群聊最大等待：一直刷也最多等这么久，`0` = 不限 | `8` |
| `private_debounce_enabled` | 私聊防抖开关 | `true` |
| `private_debounce_seconds` | 私聊防抖时长 | `1.5` |
| `private_debounce_max_wait` | 私聊最大等待，`0` = 不限 | `8` |

两个语义值得说清楚：

- **刷新**：1.5 秒内又来了新消息，就重新计时（群里按「会话 + 发送者」各算各的，别人插话不会把你的计时顶掉）。
- **上限**：光刷新不够 —— 对方只要一直发，「没人说话」这个条件永远不成立。所以从这一轮第一条消息开始掐表，到 `max_wait` 必须判断一次。

私聊防抖只做合并，不做判断：1.5 秒内连发的几句会拼成一句交给 LLM，前面几条 `stop_event()` 掉（既不进 LLM 也不发消息）。`/` 开头的命令不等待。

### 图片

| 项 | 说明 | 默认 |
|---|---|---|
| `enable_image` | 是否让插件自己转述「图/文字分开发」的图片 | `false` |
| `image_caption_provider` | 转述用哪个视觉模型；空 = 跟随 AstrBot `provider_settings` | 空 |

> 图片转述的**提示词**跟随 AstrBot 的 `provider_settings.image_caption_prompt`，不单独配置。
> 只有主模型本身支持图片时（`modalities` 含 `image`）才需要开；纯文本模型开了也白花视觉模型的调用费。

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
- 私聊：**0**（只做合并，不调模型）
- 平时没有关键词、不在窗口、没有私聊消息：**完全不调用**

## 常见问题

**Q：她怎么知道「图」？**
A：插件在历史里给图片留一个 `[图片]` 占位（不带内容，省 token），让判断模型知道「刚才有人发了图」。她真回复时，图的文字描述由 AstrBot 自带的群聊上下文注入。

**Q：为什么发完图要等几秒她才看得见？**
A：AstrBot 的图片转述是**同步**的（`group_chat_context` 里 `await` 视觉模型），一张图 3~8 秒。发完图马上叫她，图可能还没转述完。

**Q：能不发关键词、只靠小模型判断吗？**
A：可以，把 `keywords` 清空、`auto_keywords` 关掉。但那样每条群消息都会调一次小模型，成本会上去。

**Q：和 `group_chat_plus`、`persona_router` 冲突吗？**
A：不冲突。本插件不碰 `system_prompt` / `contexts` / `request_llm`。

**Q：为什么私聊连发三句只回一次？**
A：这是 `private_debounce_enabled` 有意为之 —— 分开回三次很吵。想要每条都回，把它关掉。

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
- 与 `astrbot_plugin_debounce` 功能重叠：那个按会话去重、新消息会取消前一条的回复，多人同时说话时会互相挤掉；本插件的防抖按「会话 + 发送者」独立，**建议二选一，都开容易互相抵消**
- 与 `astrbot_plugin_reply_merge` 的 thinking window 叠加时，唤醒消息可能被它 `stop_event()` 掉并合并进上一轮（表现为「叫了没单独回」）
- 被 @ 时 `is_at_or_wake_command` 本来就是 true，本插件不重复处理
- AstrBot 自带概率回复在 `is_at_or_wake_command = True` 时会跳过，所以不会双回

## 更新日志

### v1.1.8

- **删掉遗留的调试钩子**：`_probe_waiting` 会给**每次** LLM 请求打一条 INFO，是定位唤醒问题时留下的，正常运行时纯属噪音
- **`terminate()` 补清理** `_private_pending` / `_debounce_start`：卸载插件时这两个 dict 之前会留着
- **防抖起点自愈**：`_debounce_start` 记录一轮防抖的起始时刻，正常情况下由最后放行的那条消费掉。万一上一轮异常收尾没走到清理，起点会一直留着，下次同一个人隔很久说话时算出的剩余额度是负数 → 直接 0 秒判断、防抖失效。现在超过 `max_wait × 2` 的陈旧起点会被重置
- **私聊开关关掉时清理残留**：`_private_pending` 里攒着的文本之前没人取，下次开回来会被合并进新消息
- **默认值与配置页对齐**：`window_seconds` 120→60、`reply_probability` 0.8→1.0、`history_count` 8→10、`enable_image` true→false、`debounce_seconds` 2.5→1.5、`debounce_outside` 0→2.5。之前两边不一致，用户删掉配置项时的行为跟 WebUI 显示的默认值对不上

### v1.1.7

- **新增防抖最大等待** `debounce_max_wait` / `private_debounce_max_wait`（默认 8 秒，`0` = 不限）。防抖的语义是「等到没人说话」，但对方一直发这个条件就永远不成立，她会一整晚不开口。现在从这一轮第一条消息掐表，到点强制判断

### v1.1.6

- **新增私聊防抖**：`private_debounce_enabled` / `private_debounce_seconds`。之前 wake 只挂了群消息，私聊是裸的 —— 空闲时连发三条会各自进 LLM 回三次
- **新增群聊防抖开关** `group_debounce_enabled`
- **修 token 撞值**：防抖 token 原来用 `time.monotonic()`，Windows 上精度只有约 15.6ms，三条消息在 40ms 内到达时 token 会取到同一个值 —— 先到的醒来「token 等于自己」就放行并把后面几条全合并，后到的 pop 出空列表却仍被放行，本该被吞掉的消息反而进了多次 LLM。改成单调自增序号

### v1.1.5

- **防抖按「会话 + 发送者」独立**：token 的 key 原来只有群号，A、B 在 1.5 秒内先后叫她，B 的 token 会把 A 的顶掉，A 那条直接放弃 —— 不唤醒也不回复。现在各算各的，同一个人连发仍然只判最后一条

### v1.1.4

- **修复历史越轮累积**：`_split_history_tail` 只按发言人连续匹配，上一轮没成功回复时「你」不会打断连续性，同样的内容下一轮会被合并成「大肥鱼你在吗大肥鱼你在吗」。现在超过 `HISTORY_MERGE_WINDOW`（60 秒）的旧发言不再参与合并
- **修复窗口内他人插话导致的张冠李戴**：window 场景原来无条件把判断对象换成最新那条消息（可能是别人的），`_wake` 还会把别人的话写回自己这条的 `message_str`。现在只有「同一个人继续补充」才换

### v1.1.3

- **修复「叫了不回复」**：防抖醒来后把判断对象换成了更新的那条消息，唤醒标志也打在它身上 —— 但那条消息的 pipeline 早已走完、已经在 process 阶段判定「非 @、无唤醒标志」而沉默退出，事后置位不生效；真正还在 handler 里 `await` 的是发起防抖的那条，它没被置位。现在判断仍用最新的合并内容，唤醒标志打在发起的那条上

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
- [ ] 确认 `.gitignore` 已排除 `__pycache__/` 和 `*.bak-*`
