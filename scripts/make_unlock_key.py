import argparse
import hashlib
import secrets


def main():
    parser = argparse.ArgumentParser(description="生成 pytrade.key 试用解除文件")
    parser.add_argument("--secret", help="开发者自留的解锁口令；缺省时随机生成（随机口令仅本次有效，需妥善保存）")
    parser.add_argument("--out", default="pytrade.key")
    args = parser.parse_args()
    from pathlib import Path

    secret = args.secret or secrets.token_hex(32)
    digest = hashlib.sha256(secret.encode()).hexdigest()
    Path(args.out).write_text(digest, encoding="utf-8")
    print(f"已写入 {args.out}（内容为摘要，不包含口令本身）")
    print(f"请将 src/pytrade/trial.py 中的 _UNLOCK_DIGEST 更新为：{digest}")
    print("pytrade.key 已在 .gitignore 中，不要提交或随包分发")


if __name__ == "__main__":
    main()
