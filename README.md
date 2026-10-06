# Ark Web Search — Home Assistant 联网搜索与本地内容 LLM Tools

给 Home Assistant Assist 提供一个 LLM API，包含七个 Tool：联网搜索、故事库
列表/获取、英文分级阅读库列表/获取、脑筋急转弯与冷笑话库列表/获取。
一个 Conversation Agent 只需勾选一次 `ark_web_search`，即可同时获得联网搜索、
讲故事、英语陪练和猜谜互动能力。

## 设计要点

- **不侵入任何会话代理**。通过 HA 原生 `llm.async_register_api()` 注册为一个
  LLM API（id = `ark_web_search`），任何支持 `llm_hass_api` 的集成
  （`local_openai` / OpenAI / Anthropic / Google / AI Task）都能在配置项里勾选，
  和 `assist`、`memory` 并列。
- **搜索**调用火山引擎豆包搜索 Custom 版；**内容**来自本地素材文件，
  不经过网络。
- API Key 通过 config flow UI 录入，存于 config entry，不进 YAML。

## 安装

1. 把 `custom_components/ark_web_search/` 拷到 HA 的
   `/config/custom_components/`。
2. 把素材目录放到 HA media 目录：

   ```text
   /config/media/reachy_content/
   ├── stories/
   │   ├── catalog.json
   │   └── content/          # 故事 Markdown 原文
   ├── english/
   │   ├── catalog.json
   │   └── content/          # 英文阅读 Markdown 原文
   └── riddles/
       ├── catalog.json
       └── content/          # 谜面、选项与答案 Markdown
   ```

3. 重启 HA，在「设置 → 设备与服务 → 添加集成」搜索 **Ark Web Search**，
   填入 Ark API Key。
4. 在 Conversation Agent 的配置里勾选 LLM API `ark_web_search`。

素材目录可用 `tools/build_catalog.py` 从 Markdown 源文件构建：

```bash
export ARK_API_KEY=... ARK_MODEL=doubao-seed-2-0-mini-260428
python3 tools/build_catalog.py stories --src <源目录> --out <输出目录>/stories \
  --whitelist tools/story_quality_whitelist.json
python3 tools/build_catalog.py english --src <源目录> --out <输出目录>/english
python3 tools/build_riddles_catalog.py --src <下载数据目录> --out <输出目录>/riddles \
  --whitelist tools/riddle_quality_whitelist.json
```

不设置 `ARK_API_KEY` 时使用确定性启发式生成摘要与主题；设置后调用 Ark
Chat Completions 生成，结果缓存在输出目录的 `.enrichment_cache.json`。

## Tool 契约

### `web_search`

```text
query      (必填) 关键词，≤100 字，单个查询
time_range (可选) OneDay | OneWeek | OneMonth | OneYear
count      (可选) 1~10，默认 5
```

### `stories_list` / `english_list`

只返回元数据（标题、摘要、年龄、难度/级别、主题、字数），不返回正文。

```text
stories_list: query, age(1-15), difficulty(1-5), language, limit, cursor
english_list: query, age(1-15), grade(1-6), level, difficulty(1-10),
              max_words, limit, cursor
```

`limit` 最大 20；结果超过一页时返回不透明 `next_cursor`。`stories_list` 无筛选调用（无参数或仅指定 `limit`）时随机返回故事；带其他参数时保持筛选与稳定排序。

### `stories_fetch` / `english_fetch`

只接受 list 返回的 `id`，不接受文件路径。长文按 section 分页：

```text
id              (必填) list 返回的内容 ID
section_start   (可选) 起始 section，默认 0
section_count   (可选) 本次 section 数，最大 30
include_coaching (可选，仅 english_fetch) 是否返回陪练信息，默认 true
```

返回中 `has_more=true` 时用 `next_section_start` 继续读取。单次 fetch
正文最多 12000 字符（可在 integration 选项中调整，2000–20000）。

### `riddles_list` / `riddles_fetch`

`riddles_list` 返回谜面和元数据，不返回答案；可按年龄、难度、类别筛选。无筛选调用（无参数或仅指定 `limit`）时随机返回题目，带其他参数时保持筛选与稳定排序。
正式库只包含经过逐条审核的逻辑脑筋急转弯、冷笑话式脑筋急转弯和数字谜；
视觉依赖强、低质量的字谜不进入语音库。

Conversation Agent 必须先用 `riddles_list` 出一道题并等待用户猜。只有用户已经
作答，或明确说不知道、放弃、要求揭晓时，才用同一 ID 调用 `riddles_fetch`
读取答案；不可在出题回合提前获取或泄露 twist。

## 安全边界

- 只接受内容 ID，参数不接受文件路径；`catalog.json` 中的相对路径在加载和
  读取时都做目录穿越校验。
- 单个素材文件超过 1 MB 不加载；list 最多 20 条；fetch 正文有字符上限。
- 素材库缺失或损坏不影响搜索工具，内容工具返回结构化错误。
