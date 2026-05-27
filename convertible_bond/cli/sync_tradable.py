"""
一键同步全市场存续可转债基础信息 → 项目级 cb_data 文件.

用法:
    # 通过 WindPy 写入 data/cb_data.json
    python -m convertible_bond.cli.sync_tradable

    # 自定义 bundle 输出路径
    python -m convertible_bond.cli.sync_tradable --bundle ./my_cb_data.json

    # 仅显示当前 bundle 状态
    python -m convertible_bond.cli.sync_tradable --info

  典型刷新节奏:
    - 月初: 全量同步 (覆盖下修 / 新债上市 / 退市)
    - 临时: 单只债的 GUI 🔄 按钮 (重大事件后)
"""
import argparse
import shutil
import sys
import time
from datetime import date
from pathlib import Path

from ..cache import TermsBundle, project_bundle_path
from ..cb_data_sync import filter_listed_codes, sync_cb_terms
from ..data_providers import DataProvider, WindDataProvider


def _save_history_snapshot(bundle, bundle_path: Path) -> Path | None:
    """把当前 cb_data bundle 复制一份到 cb_data_history/YYYY-MM-DD.json."""
    from ..historical_terms import project_terms_history_dir
    history_dir = project_terms_history_dir()
    history_dir.mkdir(parents=True, exist_ok=True)
    snapshot_name = f"{date.today().isoformat()}.json"
    snapshot_path = history_dir / snapshot_name
    try:
        shutil.copy2(bundle_path, snapshot_path)
        return snapshot_path
    except OSError as exc:
        print(f"⚠️  保存历史快照失败: {exc}", file=sys.stderr)
        return None


def _make_provider(name: str) -> DataProvider:
    if name == "wind":
        return WindDataProvider()
    raise ValueError(f"未知数据源: {name}")


def main():
    parser = argparse.ArgumentParser(
        description="一键同步全市场存续可转债基础信息到项目 cb_data 文件",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__)
    parser.add_argument("--source", "-s", default="wind",
                        choices=["wind"],
                        help="静态基础信息数据源 (固定 Wind)")
    parser.add_argument("--bundle", "-b", default="",
                        help="bundle 文件路径 (默认 <repo>/data/cb_data.json)")
    parser.add_argument("--info", action="store_true",
                        help="只显示 bundle 信息, 不做同步")
    parser.add_argument("--limit", type=int, default=0,
                        help="限制最多同步 N 只 (调试用, 0=不限制)")
    parser.add_argument("--codes", nargs="*", default=[],
                        help="只同步指定代码 (绕过 list_tradable_cbs)")
    parser.add_argument("--incremental", "-i", action="store_true",
                        help="增量更新: 跳过本地条款库中近期已刷新的债, 只拉新债 / 过期债")
    parser.add_argument("--max-age-days", type=int, default=7,
                        help="增量模式下视为新鲜的最大天数 (默认 7)")
    args = parser.parse_args()

    bundle_path = Path(args.bundle) if args.bundle else project_bundle_path()
    bundle = TermsBundle(bundle_path)

    if args.info:
        meta = bundle.bundle_meta()
        bonds = bundle.list_bonds()
        print(f"Bundle 路径: {bundle.path}")
        print(f"上次更新: {meta.get('updated_at', '?')}")
        print(f"数据源: {meta.get('source', '?')}")
        print(f"债券数量: {len(bonds)}")
        if bonds:
            print(f"前 5 只: {bonds[:5]}")
        return 0

    try:
        provider = _make_provider(args.source)
    except Exception as e:
        print(f"❌ 数据源初始化失败: {e}", file=sys.stderr)
        return 2
    # 拉取存续可转债代码列表
    if args.codes:
        codes = list(args.codes)
        print(f"使用用户指定的 {len(codes)} 个代码")
    else:
        print(f"通过 {provider.name} 拉取沪深可转债成分 ...")
        try:
            items = provider.list_tradable_cbs(date.today())
        except NotImplementedError:
            print(f"❌ {provider.name} 不支持 list_tradable_cbs", file=sys.stderr)
            return 2
        except Exception as e:
            print(f"❌ 拉取存续清单失败: {e}", file=sys.stderr)
            return 3
        print(f"  → 找到 {len(items)} 只存续可转债")
        codes, dropped_private = filter_listed_codes(items)
        if dropped_private:
            print(f"  → 剔除 {len(dropped_private)} 只定向/非公募代码段")

    if args.limit > 0:
        codes = codes[:args.limit]
        print(f"  (--limit 截断至前 {len(codes)} 只)")

    if not codes:
        print("❌ 无可同步的代码", file=sys.stderr)
        return 1

    mode_label = "增量" if args.incremental else "全量"
    print(f"开始 {mode_label} 同步基础信息到 {bundle.path}")
    if args.incremental:
        print(f"  增量模式: 跳过本地条款库中 {args.max_age_days} 天内已刷新的债")
    print(f"  注: 每只债 2 次 Wind 接口调用 (基础字段 + 完整付息计划), 预计 ~{len(codes)*0.6:.0f}s")

    start = time.time()

    def progress(i, total, code):
        # 每 20 只打一行, 避免刷屏
        if (i + 1) % 20 == 0 or (i + 1) == total:
            elapsed = time.time() - start
            rate = (i + 1) / max(elapsed, 1e-6)
            eta = (total - i - 1) / max(rate, 1e-6)
            print(f"  [{i+1:>4}/{total}]  {code:<14}  {rate:.1f}/s  ETA {eta:.0f}s",
                  flush=True)

    result = sync_cb_terms(
        provider, codes, store=bundle, on_progress=progress,
        incremental=args.incremental, max_age_days=args.max_age_days)
    elapsed = time.time() - start

    success = result["success"]
    failed = result["failed"]
    dropped = result.get("dropped", [])
    skipped = result.get("skipped", [])
    parts = [f"✅ 成功 {len(success)} 只"]
    if skipped:
        parts.append(f"⏭️  跳过 {len(skipped)} 只 (近期已刷新)")
    parts.append(f"⚠️  剔除 {len(dropped)} 只 (已到期/异常)")
    parts.append(f"❌ 失败 {len(failed)} 只")
    parts.append(f"耗时 {elapsed:.1f}s")
    print(f"\n{', '.join(parts)}")
    print(f"   bundle 文件: {bundle.path}")
    # 自动保存 cb_data 历史快照, 积累后可用于策略回测防未来函数
    snapshot_path = _save_history_snapshot(bundle, bundle_path)
    if snapshot_path:
        print(f"   历史快照: {snapshot_path}")
    if dropped:
        print("\n剔除列表 (前 20):")
        for code, reason in dropped[:20]:
            print(f"  {code}: {reason}")
        if len(dropped) > 20:
            print(f"  ... 还有 {len(dropped) - 20} 只")

    if failed:
        print("\n失败列表:")
        for code, err in failed[:20]:
            print(f"  {code}: {err}")
        if len(failed) > 20:
            print(f"  ... 还有 {len(failed) - 20} 只")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
