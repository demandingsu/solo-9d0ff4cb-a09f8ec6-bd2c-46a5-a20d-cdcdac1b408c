#!/usr/bin/env python3
"""生成甲、乙两个分室示例子库，用法：

    python3 scripts/make_branch_dbs.py [输出目录，默认 ./branch_dbs]

会在目录下生成 jia.db 与 yi.db，两库表结构相同、原编号同号，
可直接在网页 /merge 工作台首次上传。
"""
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), ".."))

from app.branch_sample import create_branch_db

if __name__ == "__main__":
    outdir = sys.argv[1] if len(sys.argv) > 1 else "branch_dbs"
    os.makedirs(outdir, exist_ok=True)
    for branch, fname in (("甲", "jia.db"), ("乙", "yi.db")):
        path = os.path.join(outdir, fname)
        create_branch_db(path, branch)
        print(f"已生成{branch}分室示例库：{path}")
