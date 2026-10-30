#!/usr/bin/env python3
"""独立校验 Aura 写的哈希链账本：hash_i = sha256(prev|seq|event|data)，prev 为上一条的 hash（首条为 64 个 0）。"""
import hashlib, json, sys
def check(path):
    prev = "0" * 64; n = 0
    for i, line in enumerate(open(path)):
        r = json.loads(line)
        body = "%s|%d|%s|%s" % (prev, r["seq"], r["event"], r["data"])
        h = hashlib.sha256(body.encode()).hexdigest()
        if r["prev"] != prev or r["hash"] != h or r["seq"] != i:
            return False, i
        prev = h; n += 1
    return True, n
if __name__ == "__main__":
    ok, n = check(sys.argv[1]); print(("OK %d events" if ok else "BROKEN at %d") % n); sys.exit(0 if ok else 1)
