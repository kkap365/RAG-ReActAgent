"""在真实运行环境里验证按文档维度的缓存失效。

不经过 chat（避免检索结果变化干扰），而是直接用运行中的
RedisAnswerCache 实例算 deps_token，对比入库前后。

跑法：
    docker compose exec -T backend python /app/docver_e2e.py
"""
import json
import time
import urllib.request

BASE = "http://127.0.0.1:8000"


def req(method, path, data=None, token=None, files=None):
    headers = {}
    if token:
        headers["Authorization"] = "Bearer " + token
    body = None
    if files:
        boundary = "----e2eboundary"
        name, filename, content = files
        body = (
            f"--{boundary}\r\n"
            f'Content-Disposition: form-data; name="{name}"; filename="{filename}"\r\n'
            f"Content-Type: text/plain\r\n\r\n"
        ).encode() + content + f"\r\n--{boundary}--\r\n".encode()
        headers["Content-Type"] = f"multipart/form-data; boundary={boundary}"
    elif data is not None:
        body = json.dumps(data).encode()
        headers["Content-Type"] = "application/json"
    r = urllib.request.Request(BASE + path, data=body, headers=headers, method=method)
    with urllib.request.urlopen(r, timeout=300) as resp:
        return json.loads(resp.read())


def main():
    import os

    from rag.answer_cache import get_answer_cache

    user = os.getenv("BOOTSTRAP_ADMIN_USERNAME", "admin")
    pwd = os.getenv("BOOTSTRAP_ADMIN_PASSWORD", "")
    token = req("POST", "/api/auth/login", {"username": user, "password": pwd})["access_token"]

    stamp = str(int(time.time()))
    print(f"批次 {stamp}")

    # 上传文档 A
    doc_a = req("POST", "/api/documents/upload", token=token,
                files=("file", f"dv_a_{stamp}.txt",
                       f"苹果是一种水果，富含维生素C。苹果树长在温带。（批次{stamp}）".encode()))
    doc_a_id = doc_a["id"]
    print(f"doc_a = {doc_a_id}")

    # 等就绪
    for _ in range(60):
        docs = req("GET", "/api/documents", token=token)
        items = docs if isinstance(docs, list) else docs.get("items", [])
        if any(d.get("id") == doc_a_id and d.get("status") == "ready" for d in items):
            break
        time.sleep(2)
    print("doc_a ready")

    cache = get_answer_cache()
    srcs_a = [{"document_id": doc_a_id}]
    tok_a1 = cache.deps_token(srcs_a)
    print(f"入库 A 后，依赖 A 的 token = {tok_a1}")

    ver_a1 = cache.document_version(doc_a_id)
    print(f"docver(A) = {ver_a1}")

    # 上传文档 B
    doc_b = req("POST", "/api/documents/upload", token=token,
                files=("file", f"dv_b_{stamp}.txt",
                       f"葡萄是一种浆果，常用于酿酒。（批次{stamp}）".encode()))
    doc_b_id = doc_b["id"]
    print(f"doc_b = {doc_b_id}")

    for _ in range(60):
        docs = req("GET", "/api/documents", token=token)
        items = docs if isinstance(docs, list) else docs.get("items", [])
        if any(d.get("id") == doc_b_id and d.get("status") == "ready" for d in items):
            break
        time.sleep(2)
    print("doc_b ready")

    tok_a2 = cache.deps_token(srcs_a)
    ver_a2 = cache.document_version(doc_a_id)
    ver_b = cache.document_version(doc_b_id)
    print(f"入库 B 后，依赖 A 的 token = {tok_a2}")
    print(f"docver(A) = {ver_a2}   docver(B) = {ver_b}")

    print()
    ok = True
    if tok_a1 != tok_a2:
        print("FAIL: 入库 B 改变了依赖 A 的 token（说明失效仍是全局的）")
        ok = False
    else:
        print("PASS: 入库 B 没有改变依赖 A 的 token")

    if ver_a1 != ver_a2:
        print(f"FAIL: 入库 B 把 docver(A) 从 {ver_a1} 改成了 {ver_a2}")
        ok = False
    else:
        print(f"PASS: docver(A) 保持 {ver_a2} 不变")

    if ver_b <= 0:
        print("FAIL: docver(B) 没被初始化，说明入库没调用 bump_document_version")
        ok = False
    else:
        print(f"PASS: docver(B) = {ver_b}，入库路径已接入")

    # 再验证：bump A 之后 token 必须变
    cache.bump_document_version(doc_a_id)
    tok_a3 = cache.deps_token(srcs_a)
    if tok_a3 == tok_a2:
        print("FAIL: bump(A) 之后 token 没变")
        ok = False
    else:
        print("PASS: bump(A) 之后 token 变化，失效方向正确")

    print("\n" + ("全部通过" if ok else "存在失败项"))


if __name__ == "__main__":
    main()
