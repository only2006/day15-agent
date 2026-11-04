"""Day 15 · 工具层：给模型看的说明书 + 真正干活的函数。

四段顺序不要乱：
  1. TOOLS        工具定义（description 就是写给模型看的 prompt）
  2. get_weather  天气工具（open-meteo 免费无 Key；网络失败有兜底数据）
  3. query_db     数据库工具（SQL 三层防护 + 只读连接）
  4. dispatch     分发：模型"点菜" → 真正执行的唯一入口

单测：uv run tools.py
"""

import datetime as dt
import json
import re
import sqlite3
from pathlib import Path

import httpx

# ---------------------------------------------------------------- 1. 工具定义
TOOLS = [
    {
        "type": "function",
        "function": {
            "name": "get_weather",
            "description": (
                "查询某个城市未来几天的天气，返回降水概率、天气状况、最高最低气温。"
                "当用户问天气、要不要带伞、冷不冷时调用。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "city": {
                        "type": "string",
                        "description": "城市名称，中文或英文，例如「北京」「Shenzhen」",
                    },
                    "days": {
                        "type": "integer",
                        "description": "查询几天，1 到 3，默认 1",
                        "minimum": 1,
                        "maximum": 3,
                    },
                    "start_offset": {
                        "type": "integer",
                        "description": "从哪天开始：0=今天（默认），1=明天，2=后天",
                        "minimum": 0,
                        "maximum": 2,
                    },
                },
                "required": ["city"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_db",
            "description": (
                "对公司内部数据库执行只读查询，只能执行 SELECT。"
                "表结构：\n"
                "  employees(id, name, dept, role, city)\n"
                "    dept  部门，取值：前端 / 后端 / 测试 / 研发\n"
                "    role  岗位，取值：工程师 / 架构师 / 技术总监 / 测试主管\n"
                "  projects(id, name, owner_id, status, deadline)\n"
                "    owner_id 关联 employees.id\n"
                "注意：「后端」「前端」是部门 dept，不是 role。"
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "sql": {"type": "string", "description": "一条 SELECT 语句"}
                },
                "required": ["sql"],
            },
        },
    },
]

# ---------------------------------------------------------------- 2. 天气工具
GEO_URL = "https://geocoding-api.open-meteo.com/v1/search"
FORECAST_URL = "https://api.open-meteo.com/v1/forecast"

# 教程里说"自己补几个常用的"——这里补齐 WMO weathercode 常用值
WEATHER_CODE = {
    0: "晴朗", 1: "大部晴朗", 2: "局部多云", 3: "阴",
    45: "雾", 48: "雾凇",
    51: "小毛毛雨", 53: "毛毛雨", 55: "大毛毛雨",
    61: "小雨", 63: "中雨", 65: "大雨",
    71: "小雪", 73: "中雪", 75: "大雪",
    80: "小阵雨", 81: "阵雨", 82: "强阵雨",
    95: "雷阵雨", 96: "雷阵雨伴冰雹", 99: "强雷阵雨伴冰雹",
}


def _weather_fallback(city: str, days: int, start_offset: int, reason: str) -> dict:
    """网络不可用时给一份标注了来源的兜底数据，保证演示不会白屏。"""
    today = dt.date.today()
    rows = [
        {
            "date": (today + dt.timedelta(days=start_offset + i)).isoformat(),
            "precip_prob": 0,
            "condition": "大部晴朗（兜底数据）",
            "temp_max": 22,
            "temp_min": 14,
        }
        for i in range(days)
    ]
    return {
        "city": city,
        "source": f"fallback(网络不可用：{reason[:60]})",
        "forecast": rows,
    }


def get_weather(city: str, days: int = 1, start_offset: int = 0) -> dict:
    days = max(1, min(int(days), 3))
    start_offset = max(0, min(int(start_offset), 2))
    total = min(start_offset + days, 3)

    try:
        with httpx.Client(timeout=10.0) as client:
            geo = client.get(
                GEO_URL,
                params={"name": city, "count": 1, "language": "zh", "format": "json"},
            ).json()
            results = geo.get("results") or []
            if not results:
                return {"error": f"找不到城市：{city}"}

            lat, lon = results[0]["latitude"], results[0]["longitude"]
            name = results[0].get("name", city)

            fc = client.get(
                FORECAST_URL,
                params={
                    "latitude": lat,
                    "longitude": lon,
                    "daily": (
                        "precipitation_probability_max,weathercode,"
                        "temperature_2m_max,temperature_2m_min"
                    ),
                    "timezone": "auto",
                    "forecast_days": total,
                },
            ).json()

        daily = fc["daily"]
        rows = []
        for i in range(start_offset, total):
            rows.append(
                {
                    "date": daily["time"][i],
                    "precip_prob": daily["precipitation_probability_max"][i],
                    "condition": WEATHER_CODE.get(daily["weathercode"][i], "未知"),
                    "temp_max": daily["temperature_2m_max"][i],
                    "temp_min": daily["temperature_2m_min"][i],
                }
            )
        return {"city": name, "source": "open-meteo", "forecast": rows}

    except Exception as exc:
        # 工具永远不抛异常给模型，只返回 error / 兜底数据
        return _weather_fallback(city, days, start_offset, str(exc))


# ---------------------------------------------------------------- 3. 数据库工具
DB_FILE = Path("company.db")

FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|replace|attach|pragma)\b", re.I
)

# INSERT OR IGNORE 让建库脚本可以重复执行而不报主键冲突
SEED_SQL = """
CREATE TABLE IF NOT EXISTS employees (
    id INTEGER PRIMARY KEY, name TEXT, dept TEXT, role TEXT, city TEXT
);
CREATE TABLE IF NOT EXISTS projects (
    id INTEGER PRIMARY KEY, name TEXT, owner_id INTEGER, status TEXT, deadline TEXT
);
INSERT OR IGNORE INTO employees VALUES
    (1,'小明','前端','工程师','广州'),
    (2,'小红','后端','工程师','深圳'),
    (3,'小刚','测试','工程师','广州'),
    (4,'陈默','研发','技术总监','深圳'),
    (5,'林可','研发','架构师','深圳'),
    (6,'周雨','测试','测试主管','广州');
INSERT OR IGNORE INTO projects VALUES
    (1,'星云知识库',4,'进行中','2026-12-15'),
    (2,'支付网关重构',2,'已完成','2026-09-30'),
    (3,'登录模块优化',1,'进行中','2026-11-20');
"""


def _init_db() -> None:
    """导入时自动建库：company.db 被 .gitignore 忽略，clone 后也能直接跑。

    注意这里必须用普通连接，query_db 的 mode=ro 只能读、不能建库。
    """
    conn = sqlite3.connect(DB_FILE)
    try:
        conn.executescript(SEED_SQL)
        conn.commit()
    finally:
        conn.close()


_init_db()


def query_db(sql: str) -> dict:
    sql = (sql or "").strip().rstrip(";")
    if not sql:
        return {"error": "SQL 为空"}

    low = sql.lower()
    # ① 语法白名单
    if not (low.startswith("select") or low.startswith("with")):
        return {"error": "只允许 SELECT 查询"}
    # ② 单语句检查 + 关键字黑名单
    if ";" in sql:
        return {"error": "不允许多条语句"}
    if FORBIDDEN.search(sql):
        return {"error": "包含被禁止的关键字（写操作）"}

    try:
        # ③ 数据库层面只读——前两层都能被绕过，这一层才是真的
        conn = sqlite3.connect(f"file:{DB_FILE}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        cur = conn.execute(sql)
        rows = cur.fetchmany(50)  # 结果条数限制，防爆内存
        cols = [d[0] for d in cur.description] if cur.description else []
        conn.close()
        return {"columns": cols, "rows": [dict(r) for r in rows], "count": len(rows)}
    except sqlite3.Error as exc:
        return {"error": f"SQL 执行失败：{exc}"}


# ---------------------------------------------------------------- 4. 分发
REGISTRY = {"get_weather": get_weather, "query_db": query_db}


def dispatch(name: str, arguments: dict) -> dict:
    fn = REGISTRY.get(name)
    if fn is None:
        return {"error": f"不存在的工具：{name}"}

    # 模型常多给字段（如 unit="c"），只保留函数真正接受的参数
    allowed = fn.__code__.co_varnames[: fn.__code__.co_argcount]
    kwargs = {k: v for k, v in (arguments or {}).items() if k in allowed}

    try:
        return fn(**kwargs)
    except TypeError as exc:
        return {"error": f"参数不匹配：{exc}"}
    except Exception as exc:
        return {"error": f"工具执行异常：{exc}"}


# ---------------------------------------------------------------- 5. 单测
if __name__ == "__main__":
    print("=== 天气工具 ===")
    print(json.dumps(get_weather("北京", 2), ensure_ascii=False, indent=2))
    print("\n=== 数据库工具 ===")
    print(json.dumps(
        query_db("SELECT name, dept FROM employees WHERE city='深圳'"),
        ensure_ascii=False, indent=2,
    ))
    print("\n=== 安全校验（应全部返回 error）===")
    for bad in [
        "DROP TABLE employees",
        "SELECT 1; DROP TABLE employees",
        "UPDATE employees SET name='x'",
    ]:
        print(f"  {bad!r} -> {query_db(bad)}")
    print("\n=== 分发兜底 ===")
    print(" ", dispatch("不存在的工具", {}))
