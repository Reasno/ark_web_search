# Ark Web Search — Home Assistant 联网搜索 LLM Tool

给 Home Assistant Assist 加一个 `web_search` LLM tool，底层调用
[火山引擎豆包搜索 Custom 版](https://www.volcengine.com/docs/87772/2272953)。

## 设计要点

- **不侵入任何会话代理**。通过 HA 原生 `llm.async_register_api()` 注册成一个
  LLM API（id = `ark_web_search`，显示名 `Web Search (Ark)`），任何支持
  `llm_hass_api` 的集成（`local_openai` / OpenAI / Anthropic / Google /
  AI Task）都能在配置项里勾选，和 `assist`、`memory` 并列。
- **不走 Ark `/responses` 的服务端 web_search**，因此不会触发 Ark 服务端在
  请求里追加 `当前时间：…` 那条伪 user 消息。
- API Key 通过 config flow UI 录入，存于 `.storage/core.config_entries`，
  不进 YAML、不入库。

## 安装

把 `custom_components/ark_web_search/` 拷到 HA 的 `/config/custom_components/`，
重启后在「设置 → 设备与服务 → 添加集成」搜索 **Ark Web Search**，填入 API Key。

然后在会话代理的 `llm_hass_api` 里加上 `ark_web_search`。

## Tool 契约

```
name: web_search
args:
  query      (必填) 关键词，≤100 字，单个查询
  time_range (可选) OneDay | OneWeek | OneMonth | OneYear
  count      (可选) 1~10，默认 5
```

返回：

```json
{"query":"...","result_count":3,
 "results":[{"rank":1,"title":"...","site":"火山如意","url":"...",
             "published":"2026-08-29T...","authority":"非常权威","summary":"..."}]}
```

单条 summary 截断 800 字，整体截断 6000 字，防止撑爆 context。

## 手动测试

开发者工具 → 动作：

```yaml
action: ark_web_search.search
data:
  query: 北京今天天气
  count: 3
```

## 配额

火山账号每月免费 500 次（与 Global 版共用），超出按量计费。默认限流 10 QPS。
