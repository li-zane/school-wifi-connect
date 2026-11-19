# 代理控制完善：需求与逻辑规格

> 分支 `feat/proxy-guard`。后端由 Codex 实现，前端与界面文字由 Claude 负责，Antigravity 协助视觉打磨。
> 本文是双方的接口契约：后端按此实现，前端按此调用。若实现中必须偏离，请在交付说明里逐条列出。

## 1. 目标

代理出口出问题（节点失效、订阅过期、机场故障）时，**电脑仍保持联网，校园网登录与值守程序不受影响**。
为此，本软件要能在一个界面里看清并控制代理客户端（Clash Verge Rev / FlClash）的关键状态，并在出口失效时按用户设定的顺序自动处置。

| 需求 | 现状 | 本次 |
|---|---|---|
| 订阅 | 无 | **只读显示**当前订阅名、更新时间、流量与到期（不切换订阅） |
| 节点选择 | 已有（节点弹窗） | 保留；增加当前出口延迟显示 |
| 出口可用检测 | 已有（`active_exit` + `proxy_healthy`） | 保留；记录出口延迟 |
| fallback 规则 | 只有“出口异常时一起关闭系统代理和 TUN” | **处置链**：切换节点 / 关闭 TUN / 关闭系统代理，三步可各自开关、**可调整执行顺序** |
| 系统代理与 TUN | 只能被保护功能一起关闭 | **各自独立的手动开关**；处置链中也是两个独立步骤 |

## 2. 不变的原则（实现时不得破坏）

1. **不改写客户端的配置文件**。开关一律经 `ClientSwitches`（`scripts/ProxyClient.cs` 的 UIAutomation 桥接）调用客户端自身的开关；订阅信息只读。
2. **绝不读出、返回、记录订阅 URL、密钥、文件路径、uid**。订阅只返回名称、类型、更新时间、流量、到期。
3. **只在“直连可用”时处置**：沿用 `direct_healthy()` 前置条件。直连也不通（例如校园网未登录）时不动代理设置，由登录流程先恢复联网。
4. 每次开关写入后都要用 `_verify_switch()` 核验客户端界面、持久化开关与系统实际状态一致。
5. 不引入第三方依赖（Python 3.11，无 PyYAML）。
6. 默认行为与升级前一致：`switch_node` 默认关闭，`close_tun`、`close_proxy` 默认开启。
7. 不提交、不切分支、不改版本号（见 `AGENTS.md`）。

## 3. 数据与配置

`routing-settings.json` 的 `options` 增加 `fallback`（全局，不分客户端）：

```json
"options": {
  "auto_restore": false,
  "clear_stale": false,
  "fallback": {
    "order":   ["switch_node", "close_tun", "close_proxy"],
    "enabled": {"switch_node": false, "close_tun": true, "close_proxy": true}
  }
}
```

- `order` 必须恰好是三个步骤 id 的一个排列；`enabled` 必须恰好包含三个键且值为布尔。
- 文件中缺失或格式不对时回落到上面的默认值，不报错。

## 4. API 契约

所有 POST 出错时沿用现有约定：HTTP 400 + `{"error": "面向用户的中文说明"}`。

### 4.1 `GET /api/proxy`（已有，新增字段）

```json
{
  "...": "现有字段不变",
  "exit_delay": 48,
  "fallback": {"order": ["switch_node","close_tun","close_proxy"],
               "enabled": {"switch_node": false, "close_tun": true, "close_proxy": true}},
  "last_fallback": {
    "at": "2026-11-19T08:00:00+00:00",
    "steps": [
      {"step": "switch_node", "result": "done",    "detail": "节点选择 → 日本 01（76 ms）"},
      {"step": "close_tun",   "result": "skipped", "detail": "切换节点后出口已恢复"}
    ]
  },
  "node_switch_paused": false
}
```

- `exit_delay`：最近一次健康探测的延迟（ms），未知为 `null`。
- `last_fallback`：最近一次处置链的运行记录，从未运行为 `null`。`result` 取 `done | skipped | failed`。保存在内存即可。
- `node_switch_paused`：节点切换触发频率保护（见 5.3）时为 `true`。

### 4.2 `GET /api/proxy/switches`（新增）

读取当前客户端的系统代理与 TUN 实际状态。**只做廉价读取**：注册表 + 控制器 `/configs` + 客户端持久化开关，**不调用 UIAutomation 桥接**（桥接会导航客户端界面，不适合轮询）。

```json
{
  "client": "clash-verge",
  "proxy": true,
  "tun": false,
  "proxy_foreign": false,
  "guarded": ["tun"],
  "restoring": false
}
```

- `proxy`：系统代理开启且指向本客户端端口为 `true`；关闭为 `false`；读取失败为 `null`。
- `proxy_foreign`：系统代理开启但指向其他程序（此时 `proxy` 为 `false`）。
- `tun`：控制器 `/configs` 的 `tun.enable`；控制器不可用为 `null`。
- `guarded`：当前由故障保护关闭、等待恢复的字段（恢复记录中已完成的字段）。
- `restoring`：恢复记录处于恢复中。

### 4.3 `POST /api/proxy/switch`（新增）

```json
{"client": "clash-verge", "field": "proxy", "enabled": true}
```

成功返回 4.2 的结构，另可带 `"warning": "…"`。可能耗时 5–25 秒（桥接 + 核验），前端会显示进行中状态。

逻辑（`ProxyProtection.set_switch(field, enabled)`）：

1. 校验：`field ∈ {proxy, tun}`、`enabled` 为布尔、`client` 等于当前客户端（否则 `客户端已切换，请刷新后重试`）。
2. 恢复记录处于 `restoring` 时拒绝：`代理恢复尚未完成，请先点击恢复代理`。
3. 读 GUI 开关；若已是目标状态，只核验后返回。否则 `switches.set(field, 当前值, enabled)`，再 `_verify_switch(field, enabled, {'port': mixed-port})`。
4. **手动操作即接管该字段**：若恢复记录里有这个字段，把它从 `fields / completed / attempted / restored / restore_attempted` 中移除；`fields` 为空时删除记录并 `_reset_automation()`，状态消息为 `已手动调整，继续检测出口`。
5. 开启且当前 `exit_state == 'failed'` 且故障保护开启时，返回 `warning: "出口仍异常，故障保护复核后会再次关闭"`（由 service 层判断 `proxy_fail_open`）。
6. 错误文案：桥接 `switch_disabled` 且字段为 TUN 时，改为 `TUN 开关不可用，请先在客户端中安装服务模式`；其余沿用 `ClientControlError` / `SwitchStateError` 的文案。
7. 审计标签：`切换系统代理` / `切换 TUN`，附 `· 开启` / `· 关闭`。

### 4.4 `GET /api/proxy/subscription`（新增，只读）

```json
{
  "client": "clash-verge",
  "available": true,
  "name": "我的机场",
  "kind": "remote",
  "updated_at": "2026-11-19T06:12:00+00:00",
  "usage": {"used": 45097156608, "total": 214748364800, "expire": "2026-12-31T00:00:00+00:00"},
  "message": ""
}
```

- 不可用时 `available: false`，其余字段为空/`null`，`message` 给原因：`未找到客户端订阅记录` / `当前使用本地配置` 等。
- `usage` 无流量信息时为 `null`；`total` 为 0 视为无限，`expire` 为 0 视为长期，均返回 `0` / `""`。
- 名称截断到 128 字符并去掉控制字符。
- **数据来源（只读）**：
  - **Clash Verge Rev**：`%APPDATA%\io.github.clash-verge-rev.clash-verge-rev\profiles.yaml`。根键 `current: <uid>`；`items:` 列表，每项有 `uid / type / name / updated(unix 秒) / extra{upload, download, total, expire}`，另有 `url / file / option / selected` 等——**只取需要的键，`url` 一律不解析**。`type` 为 `remote | local` 才是订阅；`used = upload + download`。需手写最小的逐行解析器，只认这一结构，解析不了就 `available: false`。
  - **FlClash**：`%APPDATA%\com.follow\clash\shared_preferences.json` → `flutter.config`（JSON 字符串）→ `currentProfileId`（int）；订阅在 `database.sqlite` 的 `profiles` 表，列 `id, type, label, last_update_date(ms), subscription_info(JSON 文本，含 upload/download/total/expire)`。用 `sqlite3.connect('file:…?mode=ro', uri=True)` 只读打开，**不要 SELECT `url` / `age_secret_key` 列**。
- 按文件 mtime 缓存，避免每次请求都解析。

### 4.5 `POST /api/proxy/fallback`（新增）

```json
{"order": ["close_tun", "switch_node", "close_proxy"],
 "enabled": {"switch_node": true, "close_tun": true, "close_proxy": false}}
```

严格校验（同第 3 节），错误文案 `故障处置设置格式不正确`。成功返回 `{"fallback": {...}}`。审计标签 `设置故障处置顺序`。

### 4.6 静态资源

`njust_wifi/server.py` 的 `ASSETS` 白名单加入 `"/proxy-controls.js": ("proxy-controls.js", "text/javascript; charset=utf-8")`。前端会新建该文件（Codex 只加白名单，不建文件）。

## 5. 处置链逻辑

### 5.1 触发

沿用 `_tick` 的判定：出口探测连续失败达到 `required_failures`（自动 3 次，手动检测 1 次）、直连可用、故障保护开关开启、出口未在探测期间变化。此时**用处置链替换原来的单次 `fail_open()`**：

```
steps = [s for s in fallback.order if fallback.enabled[s]]
若 steps 为空：state='failed'，message='代理出口异常，故障处置均未开启'
依次执行，每步前检查 cancel / allowed / switch_allowed：
  switch_node  → 成功：出口已恢复，停止后续步骤
                 跳过/失败：继续下一步
  close_tun    → fail_open(only=['tun'])，继续下一步
  close_proxy  → fail_open(only=['proxy'])，继续下一步
记录 last_fallback；通过 event 发一条汇总日志
```

顺序的含义（界面会这样解释给用户）：
- **先切换节点**：尽量不断代理；切换测速约几秒，期间流量仍走失效出口。
- **先关闭再切换**：先保住联网，再在后台换节点；开启“自动恢复”时，新节点稳定后自动恢复开关。

### 5.2 `switch_node` 步骤（`_switch_node(cancel)`）

1. 用 `active_exit()` 得到出口链（请把链保存为列表 `self.exit_chain`，不只是 `egress` 字符串）。
2. 取链上**最深的 `Selector` 组**作为目标组。没有 → `skipped`：`当前出口由客户端自动选择`。
3. 候选 = 该组 `all` 中除当前 `now`、`DIRECT / REJECT / REJECT-DROP / PASS / COMPATIBLE` 及直连/拦截类型以外的项（可以是子组）。没有 → `skipped`：`同组没有其他节点`。
4. 测速：优先 `GET /group/{组}/delay?url=https://www.gstatic.com/generate_204&timeout=3500&expected=204`；不支持时逐个 `GET /proxies/{名}/delay`（最多 12 个，可小并发）。只取 `0 < delay ≤ 3500` 的。都不通 → `failed`：`同组没有可用节点`。
5. 按延迟从低到高最多尝试 2 个：`PUT /proxies/{组}` 选中 → 回读 `now` 确认 → `proxy_healthy()` 复核。通过 → `done`，日志 `代理出口异常，已将「组」从 A 切换至 B（N ms）`。两个都没通过 → **切回原节点**，`failed`：`切换后出口仍未通过`。
6. 演示模式不执行。

### 5.3 频率保护

- 30 分钟内自动切换节点达到 3 次 → `node_switch_paused = true`，`switch_node` 步骤记 `skipped`：`节点切换过于频繁，已暂停`，继续后续步骤。
- 用户手动选择节点、修改处置设置、或 30 分钟窗口滑过后解除。

### 5.4 恢复期重试

已关闭开关、等待恢复（`_await_recovery`）时，若出口仍失败、`switch_node` 已启用且未暂停，**每 5 分钟最多尝试一次** `_switch_node`。成功后由现有的自动恢复逻辑在出口稳定后恢复开关（未开启自动恢复则只提示 `已切换节点，出口恢复，等待手动恢复代理`）。这让无人值守的机器能自愈。

### 5.5 `fail_open(only=None)` 扩展

- `only` 为要关闭的字段列表；`None` 保持原行为（关闭所有已开启字段）。
- 已有恢复记录且请求的字段不在 `fields` 中：读 GUI，该字段开启则追加到 `fields`，并把 `original[field]` 更新为当时的 GUI 值后照常关闭；`restoring` 时仍拒绝。
- 恢复记录的 `fields` 顺序即关闭顺序，`restore()` 按倒序恢复——无需改动。
- 返回值：有字段被关闭或确认已关闭为 `True`，无可关闭字段为 `False`。

## 6. 演示模式（`--demo`）

前端在演示模式下验证，后端需提供可交互的假数据，**不触碰系统**：

- `switches`：内存里的 `{'proxy': True, 'tun': False}`，`set_switch` 修改它并返回，`guarded` 为 `[]`。
- `subscription`：`{"name": "示例订阅", "kind": "remote", "updated_at": 两小时前, "usage": {"used": 42 GiB, "total": 200 GiB, "expire": 20 天后}}`。
- `fallback`：读写照常（写入演示数据目录）。
- `exit_delay`：`48`。`last_fallback`：`null`。

## 7. 测试要求

在 `tests/test_proxy_guard.py`（已存在）或新文件中补充，使用假控制器 / 假开关，覆盖：

- fallback 配置的校验、持久化、缺省回落。
- 处置链：按顺序执行；`switch_node` 成功即停止；失败继续；禁用步骤跳过；“先关闭后切换”顺序；全部禁用。
- `_switch_node`：选最深 Selector、排除 DIRECT/REJECT、两次复核失败后切回原节点、频率保护。
- `set_switch`：已是目标状态、恢复记录接管字段并在清空后删除、`restoring` 时拒绝、TUN 不可用文案。
- 订阅解析：Verge yaml 与 FlClash sqlite+prefs 各一组夹具；**断言返回值与日志中不含 url**；文件缺失/格式坏返回 `available: false`。
- 新路由的 HTTP 层测试与审计标签。
- 现有 252 个测试全部保持通过：`python -B -m pytest -q`。
