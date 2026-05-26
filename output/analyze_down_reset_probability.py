from __future__ import annotations

import argparse
import csv
import json
import re
import statistics
import time
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from datetime import date, timedelta
from pathlib import Path
from typing import Any

from convertible_bond.cninfo_provider import CninfoAnnouncementProvider
from convertible_bond.cb_events import classify_announcement_title


SEARCH_KEYS = (
    "向下修正 转股价格",
    "不向下修正 转股价格",
    "下修 转股价格",
    "触发 转股价格 向下修正 条件",
    "向下修正 转股价",
    "不向下修正 转股价",
    "下修 转股价",
)

DECISION_TYPES = {
    "down_reset_proposed",
    "down_reset_approved",
    "down_reset_rejected",
}


@dataclass
class Row:
    announcement_id: str
    event_date: date
    event_type: str
    title: str
    url: str
    bond_name: str
    issuer_code: str
    issuer_name: str
    search_keys: set[str] = field(default_factory=set)

    @property
    def bond_key(self) -> str:
        # CNINFO exposes issuer stock code reliably; title bond abbreviations are
        # often absent in proposal notices, so issuer code is the stable join key
        # for proposal -> approval episodes.
        return self.issuer_code or self.bond_name or self.issuer_name


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="抓取近 5 年转债下修公告并统计下修概率")
    parser.add_argument("--start", default="2021-05-25")
    parser.add_argument("--end", default="2026-05-25")
    parser.add_argument("--out-dir", default="output")
    parser.add_argument("--request-interval", type=float, default=0.08)
    # CNINFO currently caps each page at 30 rows even when a larger pageSize is
    # requested; using 30 keeps the pagination stop condition aligned.
    parser.add_argument("--page-size", type=int, default=30)
    parser.add_argument("--max-pages", type=int, default=50)
    parser.add_argument("--query-retries", type=int, default=4)
    return parser.parse_args()


def parse_date(value: str) -> date:
    y, m, d = (int(part) for part in value.split("-"))
    return date(y, m, d)


def month_ranges(start: date, end: date) -> list[tuple[date, date]]:
    ranges: list[tuple[date, date]] = []
    cur = date(start.year, start.month, 1)
    while cur <= end:
        if cur.month == 12:
            nxt = date(cur.year + 1, 1, 1)
        else:
            nxt = date(cur.year, cur.month + 1, 1)
        lo = max(start, cur)
        hi = min(end, nxt - timedelta(days=1))
        if lo <= hi:
            ranges.append((lo, hi))
        cur = nxt
    return ranges


def clean_title(title: str) -> str:
    title = re.sub(r"</?em>", "", str(title or ""))
    return re.sub(r"\s+", "", title).strip()


def extract_bond_name(title: str) -> str:
    patterns = (
        r"[“\"《]([^”\"》]{1,20}?(?:转债|定转|可转债))[”\"》]",
        r"([A-Za-z0-9\u4e00-\u9fa5]{1,12}(?:转债|定转))(?:转股价格|的转股价格|可转债)",
        r"([A-Za-z0-9\u4e00-\u9fa5]{1,12}(?:转债|定转))",
    )
    for pattern in patterns:
        match = re.search(pattern, title)
        if match:
            name = match.group(1)
            for token in ("向下修正", "预计触发", "触发", "关于", "提议", "建议", "修正", "下修"):
                if token in name:
                    name = name.split(token)[-1]
            if name not in {"可转债", "可转换公司债券"}:
                return name
    return ""


def classify(title: str) -> str:
    title = clean_title(title)
    local = classify_announcement_title(title)
    if local in DECISION_TYPES:
        return local
    if re.search(r"(?:预计|可能|即将|已)?触发|满足", title) and re.search(
        r"(?:向下修正|下修).{0,12}(?:条件|条款)|(?:条件|条款).{0,12}(?:向下修正|下修)",
        title,
    ):
        return "down_reset_trigger_notice"
    if re.search(r"提议.{0,20}(?:向下修正|下修)", title):
        return "down_reset_proposed"
    if re.search(r"不(?:向下修正|下修)|暂不(?:向下修正|下修)", title):
        return "down_reset_rejected"
    if re.search(r"(?:向下修正|下修).{0,20}转股价|转股价.{0,20}(?:向下修正|下修)", title):
        return "down_reset_approved"
    return "unknown"


def fetch_rows(
    provider: CninfoAnnouncementProvider,
    *,
    start: date,
    end: date,
    query_retries: int = 4,
) -> dict[str, Row]:
    rows_by_id: dict[str, Row] = {}
    ranges = month_ranges(start, end)
    total_queries = len(ranges) * len(SEARCH_KEYS)
    done = 0
    for lo, hi in ranges:
        se_date = f"{lo.isoformat()}~{hi.isoformat()}"
        for key in SEARCH_KEYS:
            done += 1
            rows = []
            for attempt in range(1, max(1, query_retries) + 1):
                try:
                    rows = provider._query_pages(
                        stock="",
                        se_date=se_date,
                        column="",
                        category="",
                        searchkey=key,
                    )
                    break
                except Exception as exc:
                    if attempt >= max(1, query_retries):
                        raise
                    wait = 2.0 * attempt
                    print(
                        f"[retry {attempt}/{query_retries}] {se_date} {key}: {exc}; wait {wait:.0f}s",
                        flush=True,
                    )
                    time.sleep(wait)
            print(
                f"[{done:>3}/{total_queries}] {se_date} {key}: {len(rows)}",
                flush=True,
            )
            for item in rows:
                title = clean_title(item.get("title", ""))
                event_type = classify(title)
                if event_type == "unknown":
                    continue
                raw: dict[str, Any] = item.get("raw") or {}
                ann_id = str(raw.get("announcementId") or item.get("url") or f"{item.get('date')}|{title}")
                existing = rows_by_id.get(ann_id)
                if existing:
                    existing.search_keys.add(key)
                    continue
                event_date = item["date"]
                if not isinstance(event_date, date):
                    continue
                row = Row(
                    announcement_id=ann_id,
                    event_date=event_date,
                    event_type=event_type,
                    title=title,
                    url=item.get("url") or "",
                    bond_name=extract_bond_name(title),
                    issuer_code=str(raw.get("secCode") or ""),
                    issuer_name=str(raw.get("secName") or raw.get("tileSecName") or ""),
                    search_keys={key},
                )
                rows_by_id[ann_id] = row
            time.sleep(0.02)
    return rows_by_id


def build_episodes(rows: list[Row]) -> list[dict[str, Any]]:
    by_bond: dict[str, list[Row]] = defaultdict(list)
    for row in rows:
        if row.event_type in DECISION_TYPES:
            by_bond[row.bond_key].append(row)
    episodes: list[dict[str, Any]] = []
    for bond_key, events in by_bond.items():
        events.sort(key=lambda row: (row.event_date, row.event_type, row.announcement_id))
        pending: dict[str, Any] | None = None
        for row in events:
            if row.event_type == "down_reset_proposed":
                if pending and (row.event_date - pending["start_date"]).days <= 120:
                    pending["event_ids"].append(row.announcement_id)
                    pending["titles"].append(row.title)
                    pending["urls"].append(row.url)
                    pending["proposed_date"] = min(pending["proposed_date"], row.event_date)
                    continue
                if pending:
                    episodes.append(pending)
                pending = _new_episode(row, "proposed_pending")
                pending["proposed_date"] = row.event_date
                continue

            if row.event_type == "down_reset_approved":
                if pending and (row.event_date - pending["start_date"]).days <= 120:
                    pending["outcome"] = "approved"
                    pending["approved_date"] = row.event_date
                    pending["end_date"] = row.event_date
                    pending["event_ids"].append(row.announcement_id)
                    pending["titles"].append(row.title)
                    pending["urls"].append(row.url)
                    episodes.append(pending)
                    pending = None
                else:
                    episodes.append(_new_episode(row, "approved"))
                continue

            if row.event_type == "down_reset_rejected":
                if pending:
                    episodes.append(pending)
                    pending = None
                episodes.append(_new_episode(row, "rejected"))
        if pending:
            episodes.append(pending)
    episodes.sort(key=lambda ep: (ep["start_date"], ep["bond_key"], ep["outcome"]))
    return episodes


def _new_episode(row: Row, outcome: str) -> dict[str, Any]:
    return {
        "bond_key": row.bond_key,
        "bond_name": row.bond_name,
        "issuer_code": row.issuer_code,
        "issuer_name": row.issuer_name,
        "start_date": row.event_date,
        "end_date": row.event_date,
        "outcome": outcome,
        "proposed_date": row.event_date if row.event_type == "down_reset_proposed" else None,
        "approved_date": row.event_date if row.event_type == "down_reset_approved" else None,
        "rejected_date": row.event_date if row.event_type == "down_reset_rejected" else None,
        "event_ids": [row.announcement_id],
        "titles": [row.title],
        "urls": [row.url],
    }


def trigger_followups(rows: list[Row], episodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    decisions_by_bond: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for ep in episodes:
        decisions_by_bond[ep["bond_key"]].append(ep)
    for eps in decisions_by_bond.values():
        eps.sort(key=lambda ep: ep["start_date"])
    out: list[dict[str, Any]] = []
    for row in sorted(rows, key=lambda r: (r.event_date, r.bond_key)):
        if row.event_type != "down_reset_trigger_notice":
            continue
        follow = None
        for ep in decisions_by_bond.get(row.bond_key, []):
            delta = (ep["start_date"] - row.event_date).days
            if 0 <= delta <= 90:
                follow = ep
                break
        out.append(
            {
                "bond_key": row.bond_key,
                "bond_name": row.bond_name,
                "issuer_code": row.issuer_code,
                "issuer_name": row.issuer_name,
                "trigger_notice_date": row.event_date,
                "trigger_title": row.title,
                "trigger_url": row.url,
                "follow_outcome": follow["outcome"] if follow else "no_decision_in_90d",
                "follow_date": follow["start_date"] if follow else None,
            }
        )
    return out


def pct(num: int, den: int) -> str:
    return "NA" if den == 0 else f"{num / den:.2%}"


def write_csv(path: Path, rows: list[dict[str, Any]], fieldnames: list[str]) -> None:
    with path.open("w", encoding="utf-8-sig", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=fieldnames, extrasaction="ignore")
        writer.writeheader()
        for row in rows:
            converted = {
                key: (
                    value.isoformat()
                    if isinstance(value, date)
                    else "; ".join(value)
                    if isinstance(value, list)
                    else value
                )
                for key, value in row.items()
            }
            writer.writerow(converted)


def main() -> int:
    args = parse_args()
    start = parse_date(args.start)
    end = parse_date(args.end)
    out_dir = Path(args.out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)

    provider = CninfoAnnouncementProvider(
        request_interval=args.request_interval,
        page_size=args.page_size,
        max_pages=args.max_pages,
        timeout=15,
    )
    rows_by_id = fetch_rows(
        provider,
        start=start,
        end=end,
        query_retries=args.query_retries,
    )
    rows = sorted(rows_by_id.values(), key=lambda row: (row.event_date, row.announcement_id))
    episodes = build_episodes(rows)
    followups = trigger_followups(rows, episodes)

    event_counts = Counter(row.event_type for row in rows)
    outcome_counts = Counter(ep["outcome"] for ep in episodes)
    approved = outcome_counts["approved"]
    rejected = outcome_counts["rejected"]
    decided = approved + rejected
    proposed_pending = outcome_counts["proposed_pending"]
    proposal_lags = [
        (ep["approved_date"] - ep["proposed_date"]).days
        for ep in episodes
        if ep.get("approved_date") and ep.get("proposed_date")
    ]

    follow_counts = Counter(item["follow_outcome"] for item in followups)
    follow_decided = follow_counts["approved"] + follow_counts["rejected"]

    stamp = f"{start:%Y%m%d}_{end:%Y%m%d}"
    ann_path = out_dir / f"down_reset_cninfo_announcements_{stamp}.csv"
    ep_path = out_dir / f"down_reset_episodes_{stamp}.csv"
    trigger_path = out_dir / f"down_reset_trigger_followups_{stamp}.csv"
    report_path = out_dir / f"down_reset_probability_report_{stamp}.md"
    json_path = out_dir / f"down_reset_probability_summary_{stamp}.json"

    write_csv(
        ann_path,
        [
            {
                "announcement_id": row.announcement_id,
                "event_date": row.event_date,
                "event_type": row.event_type,
                "bond_name": row.bond_name,
                "issuer_code": row.issuer_code,
                "issuer_name": row.issuer_name,
                "title": row.title,
                "url": row.url,
                "search_keys": sorted(row.search_keys),
            }
            for row in rows
        ],
        [
            "announcement_id",
            "event_date",
            "event_type",
            "bond_name",
            "issuer_code",
            "issuer_name",
            "title",
            "url",
            "search_keys",
        ],
    )
    write_csv(
        ep_path,
        episodes,
        [
            "bond_key",
            "bond_name",
            "issuer_code",
            "issuer_name",
            "start_date",
            "end_date",
            "outcome",
            "proposed_date",
            "approved_date",
            "rejected_date",
            "event_ids",
            "titles",
            "urls",
        ],
    )
    write_csv(
        trigger_path,
        followups,
        [
            "bond_key",
            "bond_name",
            "issuer_code",
            "issuer_name",
            "trigger_notice_date",
            "trigger_title",
            "trigger_url",
            "follow_outcome",
            "follow_date",
        ],
    )

    summary = {
        "source": "cninfo",
        "window": [start.isoformat(), end.isoformat()],
        "announcements": {
            "total_classified": len(rows),
            "event_counts": dict(sorted(event_counts.items())),
        },
        "episodes": {
            "total": len(episodes),
            "outcome_counts": dict(sorted(outcome_counts.items())),
            "decided": decided,
            "approved": approved,
            "rejected": rejected,
            "approved_share_of_decided": approved / decided if decided else None,
            "rejected_share_of_decided": rejected / decided if decided else None,
            "proposed_pending": proposed_pending,
        },
        "proposal_to_approval_lag_days": {
            "n": len(proposal_lags),
            "median": statistics.median(proposal_lags) if proposal_lags else None,
            "mean": statistics.mean(proposal_lags) if proposal_lags else None,
        },
        "explicit_trigger_followups_90d": {
            "trigger_notices": len(followups),
            "counts": dict(sorted(follow_counts.items())),
            "decided": follow_decided,
            "approved_share_of_decided": (
                follow_counts["approved"] / follow_decided if follow_decided else None
            ),
        },
        "outputs": {
            "announcements_csv": str(ann_path),
            "episodes_csv": str(ep_path),
            "trigger_followups_csv": str(trigger_path),
            "report_md": str(report_path),
        },
    }
    json_path.write_text(json.dumps(summary, ensure_ascii=False, indent=2, default=str), encoding="utf-8")

    report = "\n".join(
        [
            "# 近 5 年可转债下修触发后公司下修概率",
            "",
            f"- 样本窗口: {start.isoformat()} 至 {end.isoformat()}",
            "- 数据源: 巨潮资讯网公告搜索接口；按月切分关键词抓取后去重。",
            "- 主口径: 将同一发行主体/转债名称 120 天内的“提议下修→通过下修”合并为一轮 episode；"
            "分母只使用已有终态的 `approved + rejected`。",
            "",
            "## 公告事件计数",
            "",
            *[
                f"- {key}: {value}"
                for key, value in sorted(event_counts.items())
            ],
            "",
            "## Episode 统计",
            "",
            f"- 已终态 episode: {decided}",
            f"- 下修通过/实施: {approved}",
            f"- 不下修: {rejected}",
            f"- 仍仅看到提议、未见终态: {proposed_pending}",
            f"- 触发/决策后实际下修率: {approved}/{decided} = {pct(approved, decided)}",
            "",
            "## 提议后通过滞后",
            "",
            f"- 有提议且后续通过的 episode: {len(proposal_lags)}",
            f"- 中位滞后天数: {statistics.median(proposal_lags) if proposal_lags else 'NA'}",
            f"- 平均滞后天数: {statistics.mean(proposal_lags):.1f}" if proposal_lags else "- 平均滞后天数: NA",
            "",
            "## 显式触发提示公告的 90 日跟踪",
            "",
            f"- 触发/预计触发提示公告: {len(followups)}",
            *[
                f"- {key}: {value}"
                for key, value in sorted(follow_counts.items())
            ],
            f"- 90 日内已有终态的显式触发样本下修率: "
            f"{follow_counts['approved']}/{follow_decided} = {pct(follow_counts['approved'], follow_decided)}",
            "",
            "## 输出文件",
            "",
            f"- 公告明细: `{ann_path}`",
            f"- episode 明细: `{ep_path}`",
            f"- 显式触发跟踪: `{trigger_path}`",
            f"- 机器摘要: `{json_path}`",
            "",
            "## 口径限制",
            "",
            "- 这是公告口径，不是逐日行情精确回放口径；没有公告的潜在触发不会进入主分母。",
            "- 巨潮关键词搜索可能受标题措辞影响；脚本保留公告 URL 便于抽样复核。",
            "- `approved` 按公告终态计数；`proposed_pending` 未纳入主分母。",
        ]
    )
    report_path.write_text(report, encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
