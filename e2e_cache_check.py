# -*- coding: utf-8 -*-
"""端到端验证：按文档维度的缓存失效。

验证目标：
  入库文档 B 之后，针对文档 A 的旧提问仍然命中缓存（旧代码会全部失效）。

步骤：
  1. 登录拿 token
  2. 上传文档 A（关于苹果）
  3. 提一个问题 Q1，再提一次 Q1 —— 建立缓存
  4. 记录 hits/misses
  5. 上传文档 B（关于葡萄，内容与 Q1 无关）
  6. 再提一次 Q1 —— 关键：应该仍然命中
  7. 对比 hits/misses

跑法（在项目根目录）：
    python e2e_cache_check.py
"""
import json
import sys
import time
import urllib.request

BASE = "http://127.0.0.1:8000"
ROOT = r"D:\zero-to-tech-v2\RAG-ReActAgent"


def _load_env():
    """从容器实际环境读管理员账号，避免 .env 与 compose 插值不一致。"""
    import subprocess

    def _get(key, default=""):
        out = subprocess.run(
            ["docker", "compose", "exec", "-T", "backend", "printenv", key],
            capture_output=True, text=True, cwd=ROOT,
        )
        return (out.stdout or "").strip() or default

    return _get("BOOTSTRAP_ADMIN_USERNAME", "admin"), _get("BOOTSTRAP_ADMIN_PASSWORD", "")


def req(method, path, data=None, token=None, raw=False, files=None):
    url = BASE + path
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = None
    if files:
        boundary = "----e2eboundary1234"
        name, filename, content = files
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: text/plain\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif data is not None:
        body = json.dumps(data, ensure_ascii=False).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(url, data=body, headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=180) as resp:
        payload = resp.read()
        if raw:
            return resp.status, payload
        try:
            return resp.status, json.loads(payload)
        except Exception:
            return resp.status, payload


def redis_exec(container_cmd):
    """在 redis 容器里执行一条 redis-cli；用 docker CLI 调。"""
    import subprocess
    out = subprocess.run(container_cmd, shell=True, capture_output=True, text=True)
    return out.stdout.strip()


def cache_stats():
    hits = redis_exec('docker compose exec -T redis redis-cli GET "rag:acache:stats:hits"')
    misses = redis_exec('docker compose exec -T redis redis-cli GET "rag:acache:stats:misses"')
    return (hits or "0").strip(), (misses or "0").strip()


def wait_ready(doc_id, token, timeout=120):
    t0 = time.time()
    while time.time() - t0 < timeout:
        _, docs = req("GET", "/api/documents", token=token)
        items = docs if isinstance(docs, list) else docs.get("items", [])
        for d in items:
            if d.get("id") == doc_id:
                st = d.get("status")
                if st == "ready":
                    return True
                if st == "failed":
                    print(f"   文档处理失败: {d.get('error_message')}")
                    return False
        time.sleep(2)
    return False


def upload(filename, text, token):
    content = text.encode("utf-8")
    _, doc = req("POST", "/api/documents/upload", token=token,
                 files=("file", filename, content))
    return doc.get("id")


def chat(message, token):
    """发起一次提问；流式响应直接读完丢弃，只关心缓存计数器。"""
    try:
        req("POST", "/api/chat", data={"message": message}, token=token, raw=True)
        return True
    except Exception as e:
        print(f"   chat 失败: {e}")
        return False


def main():
    user, pwd = _load_env()
    print(f"登录用户: {user}")

    _, login = req("POST", "/api/auth/login",
                   data={"username": user, "password": pwd})
    token = login.get("access_token") or login.get("token")
    if not token:
        print("登录失败:", login)
        sys.exit(1)
    print("登录成功\n")

    stamp = str(int(time.time()))
    print("=== 步骤 1：上传文档 A（苹果）===")
    doc_a = upload(f"e2e_apple_{stamp}.txt",
                   f"苹果是一种水果，富含维生素C和膳食纤维。苹果树在温带地区广泛种植，"
                   f"常见品种有红富士和嘎啦果。（测试批次 {stamp}）", token)
    print(f"   doc_a = {doc_a}")
    if not wait_ready(doc_a, token):
        print("   文档 A 未就绪，终止")
        sys.exit(1)
    print("   文档 A 已就绪\n")

    h0, m0 = cache_stats()
    print(f"=== 步骤 2：提问前基线 hits={h0} misses={m0} ===\n")

    Q1 = "苹果富含哪些营养？"

    print("=== 步骤 3：第一次提问（预期 miss）===")
    chat(Q1, token)
    h1, m1 = cache_stats()
    print(f"   hits={h1} misses={m1}\n")

    print("=== 步骤 4：同一问题再问一次（预期 hit）===")
    chat(Q1, token)
    h2, m2 = cache_stats()
    print(f"   hits={h2} misses={m2}\n")

    print("=== 步骤 5：上传文档 B（葡萄，与 Q1 无关）===")
    doc_b = upload(f"e2e_grape_{stamp}.txt",
                   f"葡萄是一种浆果，常用于酿酒。葡萄藤需要支架生长，"
                   f"常见品种有巨峰和阳光玫瑰。（测试批次 {stamp}）", token)
    print(f"   doc_b = {doc_b}")
    if not wait_ready(doc_b, token):
        print("   文档 B 未就绪，终止")
        sys.exit(1)
    print("   文档 B 已就绪\n")

    print("=== 步骤 6：入库 B 之后，再问原来的 Q1 ===")
    print("    旧实现：全局版本号被 bump，Q1 缓存失效 -> miss")
    print("    新实现：Q1 不依赖 B，缓存仍有效 -> hit")
    chat(Q1, token)
    h3, m3 = cache_stats()
    print(f"   hits={h3} misses={m3}\n")

    print("=== 结论 ===")
    print(f"   基线        hits={h0}  misses={m0}")
    print(f"   首次提问后  hits={h1}  misses={m1}")
    print(f"   重复提问后  hits={h2}  misses={m2}")
    print(f"   入库 B 后   hits={h3}  misses={m3}")

    try:
        hit2, miss2 = int(h2), int(m2)
        hit3, miss3 = int(h3), int(m3)
        if hit3 > hit2 and miss3 == miss2:
            print("\n   PASS：入库 B 后 Q1 仍命中，缓存未被无关文档打掉")
        elif miss3 > miss2:
            print("\n   FAIL：入库 B 后 Q1 变成 miss，说明仍在全局失效")
        else:
            print("\n   结果不明确，请人工看日志")
    except ValueError:
        print("\n   计数器非数字，请人工核对")


if __name__ == "__main__":
    main()
