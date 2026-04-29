"""Daily report markdown (T16).

Output path is ``output/{date}/daily_report.md`` — sibling of the per-SKU
folders, *never* nested inside one. Statistics deliberately separate
``success`` task count from "videos actually on disk", per the operations
doc.
"""

from __future__ import annotations

import sqlite3
from datetime import date as date_cls
from pathlib import Path
from typing import Optional

from app.db.connection import connect
from app.utils.paths import daily_report_path


def _resolve_date(report_date: Optional[str]) -> date_cls:
    if report_date is None:
        return date_cls.today()
    return date_cls.fromisoformat(report_date)


def _fetch_overview(conn: sqlite3.Connection, day: date_cls) -> dict:
    rows = conn.execute(
        """
        SELECT status, COUNT(*) AS n,
               SUM(CASE WHEN downloaded_count > 0
                         AND downloaded_count < target_count THEN 1 ELSE 0 END)
                   AS partial_n
          FROM tasks
         GROUP BY status
        """
    ).fetchall()
    totals = {row["status"]: row["n"] for row in rows}
    partials_by_status = {row["status"]: (row["partial_n"] or 0) for row in rows}
    total = sum(totals.values())

    success = totals.get("success", 0)
    failed = totals.get("failed", 0)
    download_failed = totals.get("download_failed", 0)
    retry_waiting = totals.get("retry_waiting", 0)
    manual_review = totals.get("manual_review", 0)

    partial_with_output = (
        partials_by_status.get("failed", 0) + partials_by_status.get("download_failed", 0)
    )

    downloaded_videos = conn.execute(
        "SELECT COUNT(*) AS n FROM task_results"
    ).fetchone()["n"]

    return {
        "date": day.isoformat(),
        "total": total,
        "success": success,
        "failed": failed,
        "download_failed": download_failed,
        "retry_waiting": retry_waiting,
        "manual_review": manual_review,
        "partial_with_output": partial_with_output,
        "downloaded_videos": downloaded_videos,
    }


def _fetch_creative_summary(conn: sqlite3.Connection) -> list[dict]:
    rows = conn.execute(
        """
        SELECT creative_id, sku_id, segment_id, status,
               downloaded_count, target_count
          FROM tasks
         ORDER BY creative_id ASC, sequence_index ASC, segment_id ASC
        """
    ).fetchall()
    grouped: dict[str, dict] = {}
    for row in rows:
        cid = row["creative_id"]
        bucket = grouped.setdefault(cid, {"sku_id": row["sku_id"], "segments": []})
        bucket["segments"].append(
            {
                "segment_id": row["segment_id"],
                "status": row["status"],
                "downloaded": row["downloaded_count"],
                "target": row["target_count"],
            }
        )
    return [{"creative_id": cid, **data} for cid, data in grouped.items()]


def _fetch_workstation_status(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT id, today_success_count, today_failure_count, status,
                   cooldown_until, cooldown_reason
              FROM workstations
             ORDER BY id ASC
            """
        ).fetchall()
    ]


def _fetch_workstation_project_map(workstations: list) -> dict[str, str | None]:
    """Map workstation_id → flow_project_url for daily-report links."""
    return {ws.id: ws.flow_project_url for ws in workstations}


def _fetch_partial_with_output(conn: sqlite3.Connection) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT task_id, creative_id, segment_id, status,
                   downloaded_count, target_count, result_folder
              FROM tasks
             WHERE status IN ('failed', 'download_failed')
               AND downloaded_count > 0
               AND downloaded_count < target_count
             ORDER BY task_id ASC
            """
        ).fetchall()
    ]


def _fetch_error_log(conn: sqlite3.Connection, since_date: date_cls) -> list[dict]:
    return [
        dict(r)
        for r in conn.execute(
            """
            SELECT created_at, workstation_id, task_id, generation_round,
                   error_type, screenshot_path
              FROM error_logs
             WHERE created_at >= ?
             ORDER BY created_at DESC
             LIMIT 200
            """,
            (since_date.isoformat(),),
        ).fetchall()
    ]


def _render_markdown(
    *,
    overview: dict,
    creatives: list[dict],
    workstations: list[dict],
    partial_with_output: list[dict],
    errors: list[dict],
) -> str:
    out: list[str] = []
    out.append(f"# Flow Harvester Daily Report\n")
    out.append(f"日期：{overview['date']}\n")
    out.append("## 总览\n")
    out.append(f"- 总任务数（按 Segment 计）：{overview['total']}")
    out.append(
        f"- 成功任务数（downloaded_count >= target_count）：{overview['success']}"
    )
    out.append(f"- 失败任务数（`failed`）：{overview['failed']}")
    out.append(
        f"- 下载失败任务数（`download_failed`）：{overview['download_failed']}"
    )
    out.append(
        "- 「未达标但有产出」任务数（`status IN ('failed','download_failed')` "
        f"且 `0 < downloaded_count < target_count`）：{overview['partial_with_output']}"
    )
    out.append(f"- 待重试任务数：{overview['retry_waiting']}")
    out.append(f"- 人工复核任务数：{overview['manual_review']}")
    out.append(f"- 成功下载视频数（实际落盘）：{overview['downloaded_videos']}")
    out.append("")

    out.append("## 创意聚合视图\n")
    if not creatives:
        out.append("（暂无任务）\n")
    else:
        out.append("| Creative ID | SKU | Segment 进度 | 备注 |")
        out.append("|---|---|---|---|")
        for c in creatives:
            seg_summary = ", ".join(
                f"{seg['segment_id']}: {seg['downloaded']}/{seg['target']}"
                f" ({seg['status']})"
                for seg in c["segments"]
            )
            note = ""
            if any(seg["status"] in {"failed", "download_failed"} for seg in c["segments"]):
                note = "存在未达标段，可决定补跑"
            out.append(
                f"| {c['creative_id']} | {c['sku_id']} | {seg_summary} | {note} |"
            )
        out.append("")

    out.append("## 工位状态\n")
    out.append("| 工位 | 成功数 | 失败数 | 状态 | 异常原因 |")
    out.append("|---|---:|---:|---|---|")
    for ws in workstations:
        reason_parts = []
        if ws["cooldown_reason"]:
            reason_parts.append(ws["cooldown_reason"])
        if ws["cooldown_until"]:
            reason_parts.append(f"until {ws['cooldown_until']}")
        out.append(
            f"| {ws['id']} | {ws['today_success_count']} | {ws['today_failure_count']} | "
            f"{ws['status']} | {' | '.join(reason_parts) or '-'} |"
        )
    out.append("")

    out.append("## 未达标但有产出\n")
    out.append(
        "口径：任何最终态（`failed` 或 `download_failed`）中 "
        "`0 < downloaded_count < target_count`。\n"
    )
    if not partial_with_output:
        out.append("（无）\n")
    else:
        out.append("| 任务 | Creative / Segment | 进度 | 最终态 | 落盘文件 |")
        out.append("|---|---|---|---|---|")
        for t in partial_with_output:
            progress = f"{t['downloaded_count']}/{t['target_count']}"
            cs = f"{t['creative_id']} / {t['segment_id']}"
            out.append(
                f"| {t['task_id']} | {cs} | {progress} | {t['status']} | "
                f"{t['result_folder'] or '-'} |"
            )
        out.append("")

    out.append("## 异常记录\n")
    if not errors:
        out.append("（无）\n")
    else:
        out.append("| 时间 | 工位 | 任务 | 生成轮次 | 错误类型 | 截图 |")
        out.append("|---|---|---|---|---|---|")
        for e in errors:
            out.append(
                f"| {e['created_at']} | {e['workstation_id'] or '-'} | "
                f"{e['task_id'] or '-'} | {e['generation_round'] or '-'} | "
                f"{e['error_type']} | {e['screenshot_path'] or '-'} |"
            )
        out.append("")

    out.append(f"## 结果目录\n\noutput/{overview['date']}/\n")
    return "\n".join(out)


def generate_daily_report(
    *,
    db_path: Path,
    output_root: Path,
    report_date: Optional[str] = None,
) -> Path:
    day = _resolve_date(report_date)
    conn = connect(db_path)
    try:
        overview = _fetch_overview(conn, day)
        creatives = _fetch_creative_summary(conn)
        ws_rows = _fetch_workstation_status(conn)
        partials = _fetch_partial_with_output(conn)
        errors = _fetch_error_log(conn, day)
    finally:
        conn.close()

    md = _render_markdown(
        overview=overview,
        creatives=creatives,
        workstations=ws_rows,
        partial_with_output=partials,
        errors=errors,
    )
    out_path = daily_report_path(output_root, day)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(md, encoding="utf-8")
    return out_path
