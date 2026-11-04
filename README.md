# Function Calling Agent（最小可用）

一个不依赖任何 Agent 框架的 **Function Calling** 实现：模型只负责「点菜」，真正执行工具、校验参数、保证安全的是这份代码。全部逻辑就是 `agent.py` 里那个 `for turn in range(1, MAX_TURNS + 1)` 循环。

## 快速开始

```powershell
uv sync
# 配置 .env（参考 .env.example）
#   DEEPSEEK_API_KEY=<你的密钥>
#   LLM_MODEL=deepseek-chat
#   LLM_BASE_URL=https://api.deepseek.com

uv run tools.py                                  # 单测工具层（会自建 company.db）
uv run agent.py "北京明天要带伞吗" --trace
uv run agent.py "深圳有哪些后端工程师"
uv run agent.py "星云项目的负责人是谁" --trace
```

`company.db` 不需要手动建：`tools.py` 导入时自动用种子数据初始化，已被 `.gitignore` 忽略，clone 后直接能跑。

## 架构：六步循环

```
用户问题 + 工具清单  →  模型
                        ↓  tool_calls（只是"点菜"，不执行）
                   dispatch() 真正跑 Python
                        ↓  role=tool 回填结果（tool_call_id 必须对应）
                        模型  →  还要工具？回到第 2 步
                              →  不要了 = 最终答案
```

`MAX_TURNS = 5`：模型可能陷入「调工具 → 不满意 → 再调」的循环，没有上限就是无限烧钱。

## 两个工具

| 工具 | 数据源 | 要点 |
|---|---|---|
| `get_weather` | open-meteo（免费、无需 Key） | 先地理编码拿经纬度再查预报；`start_offset` 区分今天/明天/后天；网络失败返回标注来源的兜底数据，演示不会白屏 |
| `query_db` | SQLite 只读 | 允许模型写 SQL，但做了三层防护 |

### SQL 三层防护

1. **语法白名单**：只放行 `SELECT` / `WITH`
2. **单语句检查 + 关键字黑名单**：拦 `;` 和 `insert/update/delete/drop/alter/create/attach/pragma`
3. **数据库层面只读**：`sqlite3.connect("file:company.db?mode=ro", uri=True)`

> 前两层是「礼貌」，第三层才是「安全」——前两层能被绕过，只读连接由 SQLite 强制执行。另有 `fetchmany(50)` 防止 `SELECT *` 打爆内存。

参数侧还有一层：模型给的东西不能信。`dispatch()` 会过滤掉多余字段、把未知工具名和解析失败都变成 `{"error": ...}` 返回给模型（而不是抛异常），模型能读懂错误并自我修正。

## 验收实测（2026-11-05）

| 问题 | 实际发生 | 结果 |
|---|---|---|
| 北京明天要带伞吗 | `get_weather(start_offset=1)` → 2026-11-06 大部晴朗、降水 0% | 正确，没把「明天」当成今天 |
| 深圳有哪些后端工程师 | `WHERE dept='后端' AND city='深圳'` → 小红 | 正确，没把「后端」查到 `role` 列 |
| 星云项目的负责人是谁 | `LIKE '%星云%'` 关联查询 → 星云知识库 / 陈默 | 正确 |
| 今天天气怎么样（不给城市） | 主动追问城市，未调用工具 | 正确 |

单测还验证了：`DROP TABLE`、`SELECT 1; DROP TABLE x`、`UPDATE ...` 全部被拦下并返回 error；不存在的工具名有兜底。

## 今天最值得记住的三条

1. **工具设计 = 给模型写 prompt**：`description` 里不写清「dept 取值只有前端/后端/测试/研发」，模型就会查错列。两个真实 bug 都是接口设计问题，不是模型笨。
2. **控制权永远在代码手里**：执行、校验、限流、兜底都在这里，安全边界不能指望模型自觉。
3. **多轮不是重试，是带着历史再试**：模型能看到上一次的空结果，所以第二次查询是有依据的——这也是错误信息要写得让模型看懂的原因。

## 已知限制

- 内存中的对话历史不持久化，进程退出即丢失
- 限流只在单进程内有效，多实例部署需换共享存储
- `company.db` 是演示用种子数据，重启不会保留运行时写入（本来也只读）
