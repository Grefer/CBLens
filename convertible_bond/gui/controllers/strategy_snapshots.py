"""策略回测 — 快照与导出 (保存/加载/prune/删除/CSV).

从 strategy_backtest.py 按职责拆出; 经 StrategyBacktestMixin 聚合混入
CBPricerApp, 方法间通过 self.* 跨 mixin 调用不受拆分影响。
"""
from __future__ import annotations

from tkinter import filedialog, messagebox

from ...strategy_backtest import write_strategy_backtest_csv

from .strategy_common import _strategy_snapshot_jsonable, _strategy_snapshot_object_hook


class StrategySnapshotMixin:
    """策略回测 — 快照与导出 (保存/加载/prune/删除/CSV)."""

    # ── 策略回测快照 保存 / 加载 ─────────────────────────────

    _MAX_SNAPSHOTS = 8

    def _save_strategy_backtest_snapshot(self):
        """保存到 data/strategy_backtest_snapshots/ 目录, 保留最近 N 份."""
        from datetime import datetime as _dt
        result = getattr(self, "_last_strategy_bt_result", None)
        if not result:
            return None
        snap_dir = self._strategy_snapshots_dir()
        snap_dir.mkdir(parents=True, exist_ok=True)
        saved_at = _dt.now()
        payload = self._build_strategy_snapshot_payload(result, saved_at=saved_at)
        encoded = _strategy_snapshot_jsonable(payload)
        config = payload.get("meta", {}).get("config") or {}
        freq = config.get("rebalance_freq", "M")
        top_n = config.get("top_n", "?")
        ts = saved_at.strftime("%Y%m%d-%H%M%S")
        start = payload.get("meta", {}).get("start_date", "")
        end = payload.get("meta", {}).get("end_date", "")
        snapshot_id = str(payload.get("snapshot_id") or "")[:12]
        fname = f"strategy_backtest_{start}_{end}_{freq}_top{top_n}_{ts}_{snapshot_id}.json"
        path = snap_dir / fname
        self._write_strategy_snapshot_json(path, encoded)
        latest = self._strategy_snapshot_path()
        self._write_strategy_snapshot_json(latest, encoded)
        self._prune_old_snapshots()
        return {"path": path, "latest_path": latest, "snapshot_id": payload.get("snapshot_id")}

    @staticmethod
    def _write_strategy_snapshot_json(path, encoded_payload):
        import json as _json
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_suffix(".tmp")
        with open(tmp, "w", encoding="utf-8") as f:
            _json.dump(encoded_payload, f, ensure_ascii=False, indent=2)
        tmp.replace(path)

    @classmethod
    def _build_strategy_snapshot_payload(cls, result, *, saved_at):
        clean_result = cls._strategy_snapshot_result_for_save(result)
        encoded_result = _strategy_snapshot_jsonable(clean_result)
        snapshot_id = cls._strategy_snapshot_id(encoded_result)
        config = clean_result.get("config") or {}
        run_settings = clean_result.get("run_settings") or {}
        summary = clean_result.get("summary") or {}
        meta = {
            "snapshot_id": snapshot_id,
            "start_date": clean_result.get("start_date"),
            "end_date": clean_result.get("end_date"),
            "config": {
                "rebalance_freq": config.get("rebalance_freq"),
                "top_n": config.get("top_n"),
                "holding_mode": config.get("holding_mode"),
                "funding_mode": config.get("funding_mode"),
                "max_holdings": config.get("max_holdings"),
                "top_n_shortfall_policy": config.get("top_n_shortfall_policy"),  # 兼容旧快照
                "selection_view": config.get("selection_view"),
                "history_mode": config.get("history_mode"),
            },
            "run_settings": run_settings,
            "summary": {
                "final_equity": summary.get("final_equity"),
                "total_return": summary.get("total_return"),
                "max_drawdown": summary.get("max_drawdown"),
                "sharpe": summary.get("sharpe"),
                "calmar": summary.get("calmar"),
            },
            "period_count": len(clean_result.get("periods") or []),
            "equity_curve_points": len(clean_result.get("equity_curve") or []),
        }
        return {
            "schema_version": 2,
            "snapshot_id": snapshot_id,
            "saved_at": saved_at,
            "meta": meta,
            "result": clean_result,
        }

    @staticmethod
    def _strategy_snapshot_result_for_save(result):
        if not isinstance(result, dict):
            return result
        cleaned = {}
        for key, value in result.items():
            if str(key).startswith("_"):
                continue
            cleaned[key] = value
        return cleaned

    @staticmethod
    def _strategy_snapshot_id(encoded_result) -> str:
        import hashlib
        import json as _json
        raw = _json.dumps(
            encoded_result,
            ensure_ascii=False,
            sort_keys=True,
            separators=(",", ":"),
        )
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]

    def _prune_old_snapshots(self):
        snap_dir = self._strategy_snapshots_dir()
        if not snap_dir.exists():
            return
        files = sorted(snap_dir.glob("strategy_backtest_*.json"), key=lambda p: p.stat().st_mtime)
        while len(files) > self._MAX_SNAPSHOTS:
            oldest = files.pop(0)
            try:
                oldest.unlink()
            except Exception:
                pass

    def _load_strategy_backtest_snapshot(self, *, silent=False, render=True):
        """从 snapshots 目录加载所有快照到对比列表, 最新一份设为当前结果."""
        import json as _json
        snap_dir = self._strategy_snapshots_dir()
        legacy_path = self._strategy_snapshot_path()
        # 收集所有快照文件 (按修改时间排序)
        files = []
        if snap_dir.exists():
            files = sorted(snap_dir.glob("strategy_backtest_*.json"),
                           key=lambda p: p.stat().st_mtime)
        if legacy_path.exists() and legacy_path not in files:
            files.append(legacy_path)
            files = sorted(files, key=lambda p: p.stat().st_mtime)
        if not files:
            if not silent:
                from tkinter import messagebox
                messagebox.showinfo("提示", "未找到策略回测快照")
            return
        loaded_count = 0
        latest_result = None
        latest_saved_at = None
        seen_snapshot_keys: set[str] = set()
        for path in files:
            try:
                with open(path, "r", encoding="utf-8") as f:
                    payload = _json.load(f, object_hook=_strategy_snapshot_object_hook)
                result = payload.get("result")
                if not result:
                    continue
                dedupe_key = self._strategy_snapshot_dedupe_key(payload, result)
                if dedupe_key in seen_snapshot_keys:
                    continue
                seen_snapshot_keys.add(dedupe_key)
                self._patch_snapshot_drawdown(result)
                # 记录快照文件路径 (用于删除)
                result["_snapshot_id"] = dedupe_key
                result["_snapshot_path"] = str(path)
                self._record_strategy_comparison_result(result)
                latest_result = result
                latest_saved_at = payload.get("saved_at")
                loaded_count += 1
            except Exception as exc:
                print(f"[策略快照] 加载 {path.name} 失败: {exc}")
        if latest_result:
            self._last_strategy_bt_result = latest_result
            if render:
                self._render_strategy_backtest_result(latest_result)
            else:
                self._mark_strategy_tabs_dirty()
            if not silent:
                self.v_st_status.set(
                    f"已加载 {loaded_count} 份快照 (最新 {latest_saved_at or '?'})")
        elif not silent:
            self.v_st_status.set("快照文件存在但无有效数据")

    @classmethod
    def _strategy_snapshot_dedupe_key(cls, payload, result) -> str:
        snapshot_id = payload.get("snapshot_id")
        if not snapshot_id:
            meta = payload.get("meta") or {}
            snapshot_id = meta.get("snapshot_id")
        if snapshot_id:
            return str(snapshot_id)
        clean_result = cls._strategy_snapshot_result_for_save(result)
        return cls._strategy_snapshot_id(_strategy_snapshot_jsonable(clean_result))

    @staticmethod
    def _patch_snapshot_drawdown(result):
        """旧快照可能丢失 drawdown 日期, 从 equity_curve 重算补回."""
        summary = result.get("summary")
        curve = result.get("equity_curve")
        if not summary or not curve:
            return
        if summary.get("max_drawdown_start") is not None:
            return  # 已有数据, 无需修补
        try:
            from ...strategy_backtest import _drawdown_stats
            stats = _drawdown_stats(curve)
            for key in ("max_drawdown_start", "max_drawdown_end",
                        "max_drawdown_days", "longest_drawdown_days"):
                if stats.get(key) is not None:
                    summary[key] = stats[key]
        except Exception:
            pass

    @staticmethod
    def _strategy_snapshot_path():
        from pathlib import Path
        return Path(__file__).resolve().parents[3] / "data" / "strategy_backtest_snapshot.json"

    @staticmethod
    def _strategy_snapshots_dir():
        from pathlib import Path
        return Path(__file__).resolve().parents[3] / "data" / "strategy_backtest_snapshots"

    def _delete_strategy_snapshot_for_record(self, record) -> None:
        self._delete_strategy_snapshot_path(record.get("snapshot_path"))

    @staticmethod
    def _delete_strategy_snapshot_path(path) -> None:
        if not path:
            return
        from pathlib import Path
        p = Path(path)
        if p.exists():
            try:
                p.unlink()
            except Exception:
                pass

    def _delete_latest_strategy_snapshot_if_matches(self, snapshot_id) -> None:
        if not snapshot_id:
            return
        latest = self._strategy_snapshot_path()
        if not latest.exists():
            return
        try:
            import json as _json
            with open(latest, "r", encoding="utf-8") as f:
                payload = _json.load(f, object_hook=_strategy_snapshot_object_hook)
            result = payload.get("result") or {}
            latest_id = self._strategy_snapshot_dedupe_key(payload, result)
        except Exception:
            return
        if str(latest_id) == str(snapshot_id):
            self._delete_strategy_snapshot_path(latest)

    def _export_strategy_backtest_csv(self):
        if not self._last_strategy_bt_result:
            messagebox.showinfo("提示", "请先运行策略回测")
            return
        path = filedialog.asksaveasfilename(
            title="导出策略回测逐期摘要",
            defaultextension=".csv",
            filetypes=[("CSV", "*.csv"), ("所有文件", "*.*")],
            initialfile="strategy_backtest.csv",
        )
        if not path:
            return
        try:
            write_strategy_backtest_csv(path, self._last_strategy_bt_result)
            self.v_st_status.set(f"已导出策略回测到 {path}")
        except Exception as exc:
            messagebox.showerror("导出失败", str(exc))
