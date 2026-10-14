# Ark Web Search & Kids Content

Home Assistant LLM API `ark_web_search` 提供联网搜索、儿童故事、猜谜与英语阅读。
内容工具直接从本地审核素材中选取一篇并返回可朗读内容；调用过程不访问生成模型。

## 安装与配置

1. 将 `custom_components/ark_web_search` 安装到 HA 的 `/config/custom_components`。
2. 在 `/config/media/reachy_content` 下放置 `stories`、`english`、`riddles` 三个目录。
   每个目录包含 `catalog.json` 与目录内的 Markdown 素材。
3. 重启 Home Assistant 服务，在「设备与服务」添加 **Ark Web Search**，填写 Ark API Key。
4. 在对话代理或 MCP Server 的 LLM API 配置中选择 `ark_web_search`。

API Key 仅存储于 HA config entry。素材路径与内容字符上限可在集成选项中设置。
素材构建命令：

```bash
python3 tools/build_catalog.py stories --src <源目录> --out <输出目录>/stories \
  --whitelist tools/story_quality_whitelist.json
python3 tools/build_catalog.py english --src <源目录> --out <输出目录>/english
python3 tools/build_riddles_catalog.py --src <下载数据目录> --out <输出目录>/riddles \
  --whitelist tools/riddle_quality_whitelist.json
```

## 内容接口

| 工具 | 模型参数 | 返回内容 |
|---|---|---|
| `riddle_next` | 可选 `topic`、`age`、`difficulty`、`kind` | `id`、`question`、可选 `options` |
| `riddle_answer` | 可选 `id` | `answer`、可选 `explanation` |
| `story_start` | 可选 `topic`、`age`、`difficulty` | `title`、`text`、`next_cursor` |
| `story_continue` | 必填 `cursor` | `text`、`next_cursor` |
| `english_start` | 可选 `topic`、`age`、`difficulty`、`grade`、`reading_id` | `title`、`text`、`next_cursor`、`practice`、`reading_id`、`level` |
| `english_continue` | 必填 `cursor` | `text`、`next_cursor` |

- `topic`：最多 100 字符的主题或标题。没有命中时返回其他符合条件的材料，并标记
  `topic_matched=false`；代理应向用户说明。未指定主题时不返回该字段。
- `age`：1–15 岁，以年龄距离排序；返回 `age_matched` 表示是否落在素材推荐范围。
- `difficulty`：`easy | medium | hard`。故事对应素材等级 1 / 2 / 3–5，
  谜语对应 1–2 / 3 / 4–5，英语对应 1–3 / 4–6 / 7–10。
- `grade`：中国小学 1–6 年级，依次对应英语等级 1–2、2–4、3–5、5–7、7–9、9–10；
  与显式难度同时指定时取交集。
- `kind`：`brainteaser | cold_joke | number_riddle`。
- `practice`：`repeat | read_aloud | comprehension`，表示跟读、朗读或阅读理解。
- `reading_id`：英语素材的持久引用，用于保存学习进度。指定时优先于筛选条件，
  从该素材开头开始；同篇当前段落的续读使用 `next_cursor`。服务也接受素材目录的原始 ID。
- `level`：所选英语材料的数值等级（1–10），可与持久引用一起记录进度。
- `next_cursor`：下一段位置；`null` 表示结束。客户端原样传给 `cursor`，无需计算页数。

故事每段最多 700 字符，英语每段最多 240 字符；分段优先选择句子、换行与单词边界。
连续拼接各段可恢复经过 Markdown 解析的全部正文。故事按原意用中文讲述，英文素材由
对话模型忠实翻译；英语练习保持英文，每段结束等待用户。

谜题首次结果仅包含题目与选项。代理应在用户猜测、放弃或要求揭晓后调用
`riddle_answer`，不得在出题回合调用答案工具。服务端不判断用户话语是否构成揭晓授权，
该时序由代理执行。

## 会话与位置

六个接口均接受可选的 `session_id`（1–128 字符）。集成客户端应为每段对话生成随机标识，
在请求间复用并从模型参数中隐藏。Reachy 会自动完成此操作。

状态归属由 HA 认证用户和 `session_id` 共同确定；HTTP 连接重建不改变会话身份。
每个会话保存各类最近 64 个素材 ID，选择时优先使用未出现在该记录中的候选项。
标题/主题、难度、年级仍限定候选集合，候选耗尽时允许重复。

谜题 ID 为 24 位随机十六进制标识；续读游标由随机标识与固定段落位置组成。
重复同一游标返回同一段，切换题材或开启另一篇阅读不会改变已有游标的含义。
素材快照自选取起有效 30 分钟，集成最多保存 128 份素材与 256 个会话。
达到容量时淘汰最早素材及最近最少使用会话；服务重启会清空状态。

没有 `session_id` 的客户端必须显式保存谜题 ID 和续读游标；不能省略谜题 ID，
也不享有会话去重。引用仍绑定认证用户。需要对话隔离的客户端必须提供独立会话标识。

错误返回统一为 `{"error_code": "...", "message": "..."}`：

| 错误码 | 行为 |
|---|---|
| `library_unavailable` | 对应素材库不可用 |
| `no_match` | 难度、年级或类别条件无交集；可放宽条件 |
| `read_failed` | 文件缺失、超限或结构不完整；可请求另一项 |
| `invalid_reference` | 引用失效、类型错误或属于其他会话；重新开始 |
| `missing_identity` | 没有 HA 认证用户 |

## 联网搜索

`web_search` 接受必填 `query`，可选 `time_range`（`OneDay | OneWeek | OneMonth | OneYear`）
和 `count`（1–10，默认 5），返回来源、摘要与 URL。用于依赖实时外部信息的问题。

## 运行边界与验证

工具参数不接受文件路径。目录解析在加载与读取时校验；单个素材文件上限 1 MB。
某个内容库故障不影响其他内容库及搜索。内容输出仅包含交互所需正文与引用。

```bash
python3 -m unittest discover -s tests -v
```

测试覆盖会话隔离、答案分离、去重、年级边界、重试幂等、过期与容量淘汰，以及分段完整性。

Git 部署使用已推送到 `origin/main` 的提交：

```bash
python3 deploy/git-deploy.py
```

脚本通过校验主机密钥的 SSH 传输 Git bundle，在 HA 容器中检查全部素材、工具 schema
与核心测试，通过后备份组件并重启 Home Assistant 容器。API 启动失败时恢复备份。
配置、密钥与素材保留在设备；部署后的实际 MCP 交互需通过客户端验收。
