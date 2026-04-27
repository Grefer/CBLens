"""
批量从 Wind 同步转债基础信息到本地缓存的 CLI.

用法:
    # 直接列出代码
    python -m convertible_bond.cli.sync_terms 128009.SZ 113029.SH

    # 从文件读 (每行一个代码, 支持 # 注释)
    python -m convertible_bond.cli.sync_terms --file my_bonds.txt

    # 自定义缓存目录
    python -m convertible_bond.cli.sync_terms --cache-dir ./cache 128009.SZ

    # 列出当前缓存里的债
    python -m convertible_bond.cli.sync_terms --list
"""
import argparse
import sys
from pathlib import Path

from ..cache import TermsCache
from ..cb_data_sync import sync_cb_terms
from ..data_providers import DataProvider, WindDataProvider


def _make_provider(name: str) -> DataProvider:
    if name == "wind":
        return WindDataProvider()
    raise ValueError(f"未知数据源: {name}")


def _read_codes_file(path: str) -> list:
    out = []
    with open(path, "r", encoding="utf-8") as f:
        for line in f:
            line = line.split("#", 1)[0].strip()
            if line:
                out.append(line)
    return out


def main():
    parser = argparse.ArgumentParser(description="批量同步转债基础信息到本地缓存")
    parser.add_argument("codes", nargs="*", help="转债代码 (例 128009.SZ)")
    parser.add_argument("--file", "-f", help="从文件读取代码列表 (每行一个)")
    parser.add_argument("--source", "-s", default="wind",
                        choices=["wind"],
                        help="静态基础信息数据源 (固定 Wind)")
    parser.add_argument("--cache-dir", default="",
                        help="缓存根目录 (默认 ~/.cb_pricer_cache/)")
    parser.add_argument("--list", action="store_true",
                        help="只列出当前缓存里的债, 不做同步")
    args = parser.parse_args()

    cache = TermsCache(Path(args.cache_dir)) if args.cache_dir else TermsCache()

    if args.list:
        bonds = cache.list_bonds()
        print(f"缓存目录: {cache.terms_dir}")
        print(f"共 {len(bonds)} 只债:")
        for code in bonds:
            ts = cache.fetched_at(code)
            ts_str = ts.strftime("%Y-%m-%d %H:%M") if ts else "?"
            print(f"  {code:<14}  {ts_str}")
        return 0

    codes = list(args.codes)
    if args.file:
        codes.extend(_read_codes_file(args.file))
    if not codes:
        parser.error("至少提供一个代码 (位置参数) 或用 --file 指定列表")

    try:
        provider = _make_provider(args.source)
    except Exception as e:
        print(f"❌ 数据源初始化失败: {e}", file=sys.stderr)
        return 2

    print(f"开始同步 {len(codes)} 只债基础信息 (数据源: {provider.name}) → {cache.terms_dir}")

    def progress(i, total, code):
        print(f"  [{i+1:>3}/{total}] {code} ...", end="", flush=True)

    result = sync_cb_terms(provider, codes, store=cache, on_progress=progress)
    print()  # finalize last line

    success, failed, dropped = result["success"], result["failed"], result.get("dropped", [])
    print(f"\n✅ 成功 {len(success)} 只")
    if dropped:
        print(f"⚠️  剔除 {len(dropped)} 只 (已到期/异常状态):")
        for code, reason in dropped[:20]:
            print(f"  {code}: {reason}")
        if len(dropped) > 20:
            print(f"  ... 还有 {len(dropped) - 20} 只")
    if failed:
        print(f"❌ 失败 {len(failed)} 只:")
        for code, err in failed:
            print(f"  {code}: {err}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
