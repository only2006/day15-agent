"""Day 15 · Function Calling 最小可用 Agent。

用法：
    uv run agent.py "北京明天要带伞吗" --trace
    uv run agent.py "深圳有哪些后端工程师"
    uv run agent.py "星云项目的负责人是谁" --trace

六步循环（所有 Agent 框架本质都是它）：
    1. 问题 + 工具清单发给模型
    2. 模型返回 tool_calls（它只"点菜"，不执行）
    3. 我们的代码真正跑函数
    4. 结果以 role=tool 回填，tool_call_id 必须对应
    5. 再问一次模型
    6. 模型不再要工具 → 输出最终答案
"""

import argparse
import json
import os
import time

from dotenv import load_dotenv
from openai import OpenAI

from tools import TOOLS, dispatch

load_dotenv()

MODEL = os.getenv("LLM_MODEL", "deepseek-chat")
BASE_URL = os.getenv("LLM_BASE_URL", "https://api.deepseek.com")

SYSTEM_PROMPT = """你是一个严谨的助手。

规则：
1. 涉及天气、公司人员、项目信息时，必须调用工具获取真实数据，不要凭记忆回答
2. 一次可以调用多个工具；拿到结果后如果需要更多信息，可以继续调用
3. 如果工具返回空结果或报错，先判断是不是查询条件写错了（字段名、取值、大小写），
   可以换一个条件再试一次，最多重试 1 次
4. 最终回答要基于工具返回的数据，并说明数据来源
5. 工具确实查不到时，如实告诉用户，不要编造
6. 回答简洁，控制在 150 字以内
"""

MAX_TURNS = 5  # 没有上限 = 模型可能一直点菜，无限烧钱

def run(client: OpenAI, question: str, trace: bool = False) -> tuple[str, int, list[str]]:
    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": question},
    ]
    used_tools = []

    for turn in range(1, MAX_TURNS + 1):
        resp = client.chat.completions.create(
            model=MODEL, messages=messages, tools=TOOLS, temperature=0,
        )
        msg = resp.choices[0].message

        # ⑥ 不再要工具 → 这就是最终答案
        if not msg.tool_calls:
            return (msg.content or "").strip(), turn, used_tools

        # ② 把「点菜单」原样记进对话
        messages.append({
            "role": "assistant",
            "content": msg.content or "",
            "tool_calls": [tc.model_dump() for tc in msg.tool_calls],
        })

        # ③ 真正执行 ④ 回填结果
        for tc in msg.tool_calls:
            used_tools.append(tc.function.name)
            try:
                args = json.loads(tc.function.arguments or "{}")
            except json.JSONDecodeError as exc:
                result = {"error": f"参数不是合法 JSON：{exc}"}
            else:
                if trace:
                    print(f"  [turn {turn}] 调用 {tc.function.name}({json.dumps(args, ensure_ascii=False)})")
                result = dispatch(tc.function.name, args)

            if trace:
                brief = json.dumps(result, ensure_ascii=False)
                print(f"           → {brief[:160]}{'…' if len(brief) > 160 else ''}")

            messages.append({
                "role": "tool",
                "tool_call_id": tc.id,          # ⑤ 必须和 tc.id 对上
                "content": json.dumps(result, ensure_ascii=False),
            })

    return "已达到最大工具调用轮数，未能得出最终答案", MAX_TURNS, used_tools


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("question")
    parser.add_argument("--trace", action="store_true", help="打印工具调用过程")
    parser.add_argument("--repeat", type=int, default=1, help="重复几次，测稳定性")
    args = parser.parse_args()

    key = os.getenv("DEEPSEEK_API_KEY")
    if not key:
        print("没有配置 DEEPSEEK_API_KEY，请检查 .env")
        return 1

    client = OpenAI(api_key=key, base_url=BASE_URL)

    for i in range(args.repeat):
        if args.repeat > 1:
            print(f"\n===== 第 {i + 1} 次 =====")
        t0 = time.perf_counter()
        answer, turns, used = run(client, args.question, trace=args.trace)
        cost = time.perf_counter() - t0
        print(f"\n【问题】{args.question}")
        print(f"【回答】{answer}")
        print(f"【轮数】{turns}    【工具】{used or '未调用'}    【耗时】{cost:.2f}s")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())