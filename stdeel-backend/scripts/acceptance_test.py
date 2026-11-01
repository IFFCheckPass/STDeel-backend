#!/usr/bin/env python3
"""思谛 STDeel 后端验收自测。

对应 backend_requirements.md 第 8 章的 13 条验收用例。
只用标准库，可直接在服务器上对着临时实例跑：

    python3 scripts/acceptance_test.py --base-url http://127.0.0.1:8599
    python3 scripts/acceptance_test.py --base-url http://127.0.0.1:8599/stapi

注意：脚本会往目标库写入测试数据，请只对着临时实例运行。
"""

import argparse
import hashlib
import json
import sys
import uuid
from urllib import error, parse, request


# --------------------------------------------------------------------------
# 极简 HTTP 客户端
# --------------------------------------------------------------------------
class Api:
    def __init__(self, base_url: str) -> None:
        self.base = base_url.rstrip("/")
        parts = parse.urlsplit(self.base)
        self.root = f"{parts.scheme}://{parts.netloc}"

    def request(self, method: str, path: str, body=None, base: str | None = None):
        url = (base if base is not None else self.base) + path
        data = None
        headers = {"Accept": "application/json"}
        if body is not None:
            data = json.dumps(body, ensure_ascii=False).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = request.Request(url, data=data, headers=headers, method=method)
        try:
            with request.urlopen(req, timeout=20) as response:
                payload = response.read()
                status = response.status
                content_type = response.headers.get("content-type", "")
        except error.HTTPError as exc:
            payload = exc.read()
            status = exc.code
            content_type = exc.headers.get("content-type", "")
        parsed = None
        if payload and "json" in content_type:
            try:
                parsed = json.loads(payload.decode("utf-8"))
            except json.JSONDecodeError:
                parsed = None
        return status, parsed, payload

    def ok(self, method: str, path: str, body=None, base: str | None = None):
        status, parsed, payload = self.request(method, path, body, base)
        if not 200 <= status < 300:
            raise AssertionError(f"{method} {path} -> HTTP {status}: {payload[:300]!r}")
        return parsed


# --------------------------------------------------------------------------
# 断言记录
# --------------------------------------------------------------------------
class Runner:
    def __init__(self) -> None:
        self.results: list[tuple[str, bool, str]] = []

    def check(self, name: str, condition: bool, detail: str = "") -> None:
        self.results.append((name, bool(condition), detail))
        flag = "PASS" if condition else "FAIL"
        print(f"[{flag}] {name}" + (f"  ({detail})" if detail else ""))

    def summary(self) -> int:
        total = len(self.results)
        failed = [item for item in self.results if not item[1]]
        print("\n" + "=" * 72)
        print(f"验收结果：{total - len(failed)}/{total} 通过")
        for name, ok, detail in self.results:
            print(f"  {'✓' if ok else '✗'} {name}" + (f"  {detail}" if not ok and detail else ""))
        print("=" * 72)
        return 0 if not failed else 1


def find_item(items, record_id):
    for item in items:
        if item.get("id") == record_id:
            return item
    return None


def sha256_hex(text: str) -> str:
    return hashlib.sha256(" ".join(text.split()).encode("utf-8")).hexdigest()


# --------------------------------------------------------------------------
# 13 条用例
# --------------------------------------------------------------------------
def run_suite(api: Api, runner: Runner) -> None:
    run_id = uuid.uuid4().hex[:8]
    q = lambda value: parse.quote(str(value), safe="")  # noqa: E731

    # ---- 1. 首次启动：设备注册，且同一 device_id 复用同一 user_id ----
    device_id = f"test-device-{run_id}"
    first = api.ok("POST", "/users/register", {"device_id": device_id, "platform": "android"})
    user_id = first.get("user_id") or first.get("id")
    second = api.ok("POST", "/users/register", {"device_id": device_id, "platform": "android"})
    user_id_again = second.get("user_id") or second.get("id")
    runner.check(
        "1 设备注册返回非空 user_id，重复注册复用同一账号",
        bool(user_id) and user_id == user_id_again,
        f"user_id={user_id}",
    )

    # ---- 2. 用户名绑定：同名（跨设备）落到同一 user_id ----
    username = f"验收用户-{run_id}"
    name_first = api.ok("POST", "/users/register", {"username": username, "platform": "android"})
    name_second = api.ok("POST", "/users/register", {"username": username, "platform": "android"})
    name_user_id = name_first.get("user_id")
    runner.check(
        "2 用户名绑定返回稳定 user_id，同名复用",
        bool(name_user_id) and name_user_id == name_second.get("user_id"),
        f"user_id={name_user_id}",
    )

    # ---- 3. 解题上传（2 道小题）：返回 2 个数字 id，顺序一致 ----
    point_a = f"三角函数-{run_id}"
    point_b = f"两角和差-{run_id}"
    batch = {
        "user_id": user_id,
        "ai_model": "qwen-vl-max",
        "latency_ms": 3200,
        "tokens_used": 512,
        "source": "ai",
        "image_path": "/data/user/0/com.stdeel.steel/cache/x.jpg",
        "questions": [
            {
                "id": 11,
                "content": f"{run_id} 题干一：求 sin30° 的值",
                "knowledge_points": [point_a],
                "answer": "0.5",
                "solution": "过程一",
                "confidence": 0.95,
                "subject": "数学",
                "question_no": 1,
                "action_type": "solve",
                "user_feedback": "none",
            },
            {
                "id": 12,
                "content": f"{run_id} 题干二：求 cos60° 的值",
                "knowledge_points": [point_a, point_b],
                "answer": "0.5",
                "solution": "过程二",
                "confidence": 0.9,
                "subject": "数学",
                "question_no": 2,
                "action_type": "solve",
                "user_feedback": "none",
            },
        ],
    }
    created = api.ok("POST", "/solve-records", batch)
    items = created.get("items") if isinstance(created, dict) else None
    ids_ok = (
        isinstance(items, list)
        and len(items) == 2
        and all(isinstance(entry.get("id"), int) and entry["id"] > 0 for entry in items)
    )
    runner.check("3 批量上传返回等量数字 id（顺序一致）", ids_ok, f"items={items}")
    if not ids_ok:
        return
    record_one, record_two = items[0]["id"], items[1]["id"]

    # ---- 4. 标记「正确」→ PATCH → 手动同步 → GET：状态不被清空 ----
    api.ok("PATCH", f"/solve-records/{record_one}/feedback", {"feedback": "correct", "user_id": user_id})
    records = api.ok("GET", f"/solve-records?user_id={q(user_id)}&correct_days=30&wrong_days=90")
    item = find_item(records.get("items", []), record_one)
    first_sync_ok = item is not None and item.get("user_feedback") == "correct" and item.get("action_type") == "solve"
    records_again = api.ok("GET", f"/solve-records?user_id={q(user_id)}&correct_days=30&wrong_days=90")
    item_again = find_item(records_again.get("items", []), record_one)
    runner.check(
        "4 标记正确后 GET 回传 correct，再次同步不被清空",
        first_sync_ok and item_again is not None and item_again.get("user_feedback") == "correct",
        f"user_feedback={(item or {}).get('user_feedback')}",
    )

    # ---- 5. 标记「部分错误」：云端 partial，知识点 correct+1 且 wrong+1 ----
    api.ok("PATCH", f"/solve-records/{record_two}/feedback", {"feedback": "partial"})
    records = api.ok("GET", f"/solve-records?user_id={q(user_id)}&correct_days=30&wrong_days=90")
    item_two = find_item(records.get("items", []), record_two)
    mastery = api.ok("GET", f"/knowledge/mastery?user_id={q(user_id)}")
    mastery_map = {entry["knowledge_point"]: entry for entry in mastery.get("items", [])}
    stat_a = mastery_map.get(point_a, {})
    # record_one(correct) + record_two(partial) 都落在 point_a：correct=2, wrong=1
    runner.check(
        "5 部分错误落库为 partial，知识点 correct+1 且 wrong+1",
        item_two is not None
        and item_two.get("user_feedback") == "partial"
        and stat_a.get("correct_count") == 2
        and stat_a.get("wrong_count") == 1,
        f"feedback={(item_two or {}).get('user_feedback')} mastery={stat_a}",
    )

    # ---- 6. PATCH 重复调用：结果一致、不重复计数 ----
    api.ok("PATCH", f"/solve-records/{record_two}/feedback", {"feedback": "partial"})
    mastery_again = api.ok("GET", f"/knowledge/mastery?user_id={q(user_id)}")
    stat_again = {entry["knowledge_point"]: entry for entry in mastery_again.get("items", [])}.get(point_a, {})
    runner.check(
        "6 重复 PATCH 幂等（掌握度不重复计数）",
        stat_again.get("correct_count") == 2 and stat_again.get("wrong_count") == 1,
        f"mastery={stat_again}",
    )

    # ---- 7. 删除记录：DELETE 2xx、GET 不再返回、重复 DELETE 仍 2xx ----
    api.ok("DELETE", f"/solve-records/{record_one}", {"user_id": user_id})
    records = api.ok("GET", f"/solve-records?user_id={q(user_id)}&correct_days=30&wrong_days=90")
    deleted_gone = find_item(records.get("items", []), record_one) is None
    status_repeat, _, _ = api.request("DELETE", f"/solve-records/{record_one}", {"user_id": user_id})
    runner.check(
        "7 删除记录后 GET 不再返回，重复 DELETE 仍 2xx",
        deleted_gone and 200 <= status_repeat < 300,
        f"repeat_status={status_repeat}",
    )

    # ---- 8. 离线解题后联网补传：返回 1 个 id，同 local_id 不产生重复行 ----
    offline = {
        "user_id": user_id,
        "local_id": 9001,
        "question_text": f"{run_id} 离线题目：求 tan45°",
        "answer": "1",
        "solution": "过程",
        "ai_model": "qwen-vl-max",
        "latency_ms": 3200,
        "tokens_used": 512,
        "matched": False,
        "action_type": "solve",
        "user_feedback": "none",
        "image_path": "/data/x.jpg",
        "subject": "数学",
    }
    single_one = api.ok("POST", "/solve-records", offline)
    single_two = api.ok("POST", "/solve-records", offline)
    total_after = api.ok("GET", f"/solve-records?user_id={q(user_id)}")["total"]
    runner.check(
        "8 单条补传返回 1 个 id，同 local_id 重复补传不产生重复行",
        isinstance(single_one.get("id"), int)
        and single_one["id"] == single_two.get("id")
        and total_after == 2,  # 剩下的：record_two + offline 这一条
        f"id={single_one.get('id')} total={total_after}",
    )

    # ---- 9. 批量上传未拿到 id 后重传：后端去重，不产生脏数据 ----
    retry_batch = {
        "user_id": user_id,
        "ai_model": "qwen-vl-max",
        "latency_ms": 1000,
        "tokens_used": 100,
        "source": "ai",
        "image_path": "/data/retry.jpg",
        "questions": [
            {"content": f"{run_id} 批量重传题干一", "answer": "A", "solution": "S", "subject": "物理"},
            {"content": f"{run_id} 批量重传题干二", "answer": "B", "solution": "S", "subject": "物理"},
        ],
    }
    retry_first = api.ok("POST", "/solve-records", retry_batch)
    retry_second = api.ok("POST", "/solve-records", retry_batch)
    total_retry = api.ok("GET", f"/solve-records?user_id={q(user_id)}")["total"]
    runner.check(
        "9 批量重传按内容指纹去重（id 相同、行数不变）",
        retry_first.get("items") == retry_second.get("items") and total_retry == 4,
        f"ids={retry_first.get('items')} total={total_retry}",
    )

    # ---- 10. 换机 + username 绑定：能拉回历史且状态原样恢复 ----
    bound_name = f"换机用户-{run_id}"
    api.ok("POST", "/users/register", {"device_id": device_id, "username": bound_name})
    new_device = f"new-device-{run_id}"
    migrated = api.ok("POST", "/users/register", {"device_id": new_device, "username": bound_name})
    history = api.ok("GET", f"/solve-records?user_id={q(migrated.get('user_id'))}")
    partial_item = find_item(history.get("items", []), record_two)
    runner.check(
        "10 换机后按 username 拉回历史，partial/action_type 原样恢复",
        migrated.get("user_id") == user_id
        and partial_item is not None
        and partial_item.get("user_feedback") == "partial"
        and bool(partial_item.get("action_type")),
        f"new_user={migrated.get('user_id')} item={partial_item}",
    )

    # ---- 11. 答案库上传 + 匹配（knowledge_points 必须是 JSON 字符串） ----
    question_text = f"{run_id} 已知直角三角形 a=3、b=4，求斜边 c"
    api.ok(
        "POST",
        "/answer-library",
        {
            "user_id": user_id,
            "question_text": question_text,
            "question_hash": sha256_hex(question_text),
            "answer": "5",
            "solution": "由勾股定理 c=√(a²+b²)=5",
            "knowledge_points": ["勾股定理"],
            "subject": "数学",
        },
    )
    exact = api.ok(
        "POST",
        "/answer-library/match",
        {"user_id": user_id, "question_text": question_text, "question_hash": sha256_hex(question_text)},
    )
    fuzzy = api.ok(
        "POST",
        "/answer-library/match",
        {"user_id": user_id, "question_text": "第1题 " + question_text},
    )
    knowledge_points = (exact.get("answer") or {}).get("knowledge_points")
    points_ok = isinstance(knowledge_points, str)
    try:
        points_ok = points_ok and json.loads(knowledge_points) == ["勾股定理"]
    except (TypeError, json.JSONDecodeError):
        points_ok = False
    runner.check(
        "11 答案库上传 + 精确/模糊匹配，knowledge_points 为 JSON 字符串",
        exact.get("hit") is True
        and float(exact.get("similarity", 0)) >= 0.85
        and fuzzy.get("hit") is True
        and points_ok,
        f"exact={exact.get('hit')}/{exact.get('similarity')} fuzzy={fuzzy.get('hit')}/{fuzzy.get('similarity')} kp={knowledge_points!r}",
    )

    # ---- 12. /healthz 在 host 根路径可用（即使 Base URL 带前缀） ----
    root_status, root_body, _ = api.request("GET", "/healthz", base=api.root)
    prefix_path = parse.urlsplit(api.base).path
    prefixed_ok = True
    if prefix_path and prefix_path != "/":
        prefixed_status, _, _ = api.request("GET", "/healthz", base=api.base)
        prefixed_ok = 200 <= prefixed_status < 300
    runner.check(
        "12 /healthz 在 host 根路径与带前缀时都返回 2xx",
        200 <= root_status < 300 and prefixed_ok,
        f"root={root_status} prefix={prefix_path or '-'}",
    )

    # ---- 13. 缺字段容错：老客户端不带 action_type/user_feedback 也能落库并回传默认值 ----
    legacy = api.ok(
        "POST",
        "/solve-records",
        {
            "user_id": user_id,
            "ai_model": "legacy-model",
            "latency_ms": 10,
            "tokens_used": 10,
            "questions": [{"id": 5001, "content": f"{run_id} 旧客户端题干", "answer": "a", "solution": "s"}],
        },
    )
    legacy_id = (legacy.get("items") or [{}])[0].get("id")
    legacy_records = api.ok("GET", f"/solve-records?user_id={q(user_id)}")
    legacy_item = find_item(legacy_records.get("items", []), legacy_id) or {}
    runner.check(
        "13 缺字段容错：默认 subject/action_type/user_feedback/knowledge_points",
        legacy_item.get("subject") == "未分类"
        and legacy_item.get("action_type") == "solve"
        and legacy_item.get("user_feedback") == "none"
        and legacy_item.get("knowledge_points") == []
        and legacy_item.get("status") == "solve",
        f"item={legacy_item.get('subject')}/{legacy_item.get('action_type')}/{legacy_item.get('user_feedback')}",
    )

    # ---- 附加：LaTeX / Markdown 原样往返（v0.9.12 显示规范）----
    latex_content = '已知 $\\overrightarrow{AB}=(1,2)$，求 $\\overrightarrow{AB}\\cdot\\overrightarrow{AC}$。\n旧写法 \\(x^2\\)；特殊字符 a & b < c > d'
    latex_answer = '$\\mathbb{R}$ 上的解：\n$\\begin{cases} x=1 \\\\ y=2 \\end{cases}$'
    latex_solution = '$\\mathbf{a}\\cdot\\mathbf{b}=x_1x_2+y_1y_2$，$\\left| \\vec{v} \\right|$，$\\hat{x}$，$\\overline{AB}$，$\\overset{?}{=}$，$\\not=$，$\\cdots$，未知命令 $\\foo{bar}$'
    latex_posted = api.ok(
        "POST",
        "/solve-records",
        {
            "user_id": user_id,
            "ai_model": "qwen-vl-max",
            "questions": [{
                "id": 6001,
                "content": latex_content,
                "answer": latex_answer,
                "solution": latex_solution,
                "knowledge_points": ["向量"],
                "subject": "数学",
            }],
        },
    )
    latex_id = (latex_posted.get("items") or [{}])[0].get("id")
    latex_records = api.ok("GET", f"/solve-records?user_id={q(user_id)}")
    latex_item = find_item(latex_records.get("items", []), latex_id) or {}
    runner.check(
        "附加 LaTeX/Markdown 原样往返（不转义、不规范化）",
        latex_item.get("question_text") == latex_content
        and latex_item.get("answer") == latex_answer
        and latex_item.get("solution") == latex_solution,
        f"answer={latex_item.get('answer')!r}",
    )

    # ---- 附加：PUT /users/api-key 全量覆盖幂等（不输出 Key 内容） ----
    key_payload = {
        "user_id": user_id,
        "api_keys": [
            {"api_key": "sk-test-0001", "name": "通义千问", "enabled": True},
            {"api_key": "sk-test-0002", "name": "DeepSeek", "enabled": False},
        ],
    }
    api.ok("PUT", "/users/api-key", key_payload)
    api.ok("PUT", "/users/api-key", key_payload)
    keys = api.ok("GET", f"/users/api-key?user_id={q(user_id)}")
    runner.check(
        "附加 PUT /users/api-key 全量覆盖幂等",
        isinstance(keys, list) and len(keys) == 2 and {k["name"] for k in keys} == {"通义千问", "DeepSeek"},
        f"count={len(keys) if isinstance(keys, list) else keys}",
    )

    # ---- 附加：不存在的 user_id 不炸 ----
    status_unknown, body_unknown, _ = api.request("GET", "/solve-records?user_id=u_99999999")
    runner.check(
        "附加 未知 user_id 查询返回空列表而非 5xx",
        200 <= status_unknown < 300 and body_unknown.get("total") == 0,
        f"status={status_unknown}",
    )


def main() -> int:
    parser = argparse.ArgumentParser(description="STDeel 后端验收自测（13 条）")
    parser.add_argument("--base-url", default="http://127.0.0.1:8599", help="后端 Base URL，可带路径前缀")
    args = parser.parse_args()

    api = Api(args.base_url)
    runner = Runner()
    print(f"目标：{api.base}\n" + "-" * 72)
    try:
        run_suite(api, runner)
    except AssertionError as exc:
        print(f"[FAIL] 用例执行中断：{exc}")
        runner.results.append(("用例执行中断", False, str(exc)))
    return runner.summary()


if __name__ == "__main__":
    sys.exit(main())
