"""查看 cb_data 批量定价公开交易主池报告.

用法:
    python -m convertible_bond.cli.screen_pool
    python -m convertible_bond.cli.screen_pool --show-excluded 50
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

from ..batch_pricing import (
    AdmissionFilterConfig,
    DEFAULT_MIN_CREDIT_RATING,
    DEFAULT_MIN_OUTSTANDING_BALANCE,
    screen_batch_pool_from_cache,
)
from ..cache import TermsBundle, project_bundle_path


def main() -> int:
    default_min_balance = (
        DEFAULT_MIN_OUTSTANDING_BALANCE
        if DEFAULT_MIN_OUTSTANDING_BALANCE is not None
        else -1.0
    )
    parser = argparse.ArgumentParser(
        description="查看批量定价前的公开交易主池报告",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--bundle", "-b", default="",
                        help="cb_data bundle 路径 (默认 <repo>/data/cb_data.json)")
    parser.add_argument("--delist-window", type=int, default=0,
                        help=argparse.SUPPRESS)
    parser.add_argument("--min-balance", type=float, default=default_min_balance,
                        help=argparse.SUPPRESS)
    parser.add_argument("--min-rating", default=DEFAULT_MIN_CREDIT_RATING or "",
                        help=argparse.SUPPRESS)
    parser.add_argument("--min-turnover", type=float, default=-1.0,
                        help=argparse.SUPPRESS)
    parser.add_argument("--show-excluded", type=int, default=20,
                        help="展示被剔除明细数量 (默认 20)")
    args = parser.parse_args()

    bundle_path = Path(args.bundle) if args.bundle else project_bundle_path()
    bundle = TermsBundle(bundle_path)
    config = AdmissionFilterConfig(
        delist_window_days=max(0, args.delist_window),
        min_outstanding_balance=None if args.min_balance < 0 else args.min_balance,
        min_credit_rating=args.min_rating.strip() or None,
        min_turnover_amount=None if args.min_turnover < 0 else args.min_turnover,
    )
    report = screen_batch_pool_from_cache(bundle, admission_config=config)

    print(f"Bundle: {bundle.path}")
    print(f"总数: {report['total']}")
    print(f"入主池: {report['n_accepted']}")
    print(f"剔除: {report['n_excluded']}")
    if report["excluded_by_reason"]:
        print("\n剔除原因:")
        for reason, count in report["excluded_by_reason"].items():
            print(f"  {reason}: {count}")

    show = max(0, int(args.show_excluded))
    if show and report["excluded"]:
        print(f"\n剔除明细 (前 {show}):")
        for code, reason in report["excluded"][:show]:
            print(f"  {code}: {reason}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
