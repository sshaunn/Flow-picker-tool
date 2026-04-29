"""T16 — daily report tests."""

from __future__ import annotations

from datetime import date
from pathlib import Path

from app.db.connection import connect
from app.reports.daily import generate_daily_report


def _seed_mixed_tasks(db_path: Path) -> None:
    conn = connect(db_path)
    try:
        # creative_001 / A 8/8 success
        # creative_001 / B 5/8 failed (partial w/ output)
        # creative_002 / A 0/8 download_failed (no output)
        # creative_002 / B 4/8 download_failed (partial w/ output)
        # creative_003 / A 0/8 retry_waiting
        rows = [
            ("T1", "creative_001", "A", 1, 8, "success"),
            ("T2", "creative_001", "B", 2, 5, "failed"),
            ("T3", "creative_002", "A", 1, 0, "download_failed"),
            ("T4", "creative_002", "B", 2, 4, "download_failed"),
            ("T5", "creative_003", "A", 1, 0, "retry_waiting"),
        ]
        for task_id, cre, seg, idx, dl, status in rows:
            conn.execute(
                "INSERT INTO tasks (task_id, sku_id, creative_id, segment_id, "
                "sequence_index, source_asset_path, source_asset_type, video_prompt, "
                "target_count, downloaded_count, status, result_folder) "
                "VALUES (?, 'sku', ?, ?, ?, '/x', 'first_frame', 'p', 8, ?, ?, ?)",
                (task_id, cre, seg, idx, dl, status, f"/out/{cre}/{seg}"),
            )
        # results for success + partial
        for r in range(1, 3):
            for s in range(1, 5):  # 8 candidates for T1 success
                conn.execute(
                    "INSERT INTO task_results (task_id, creative_id, segment_id, "
                    "workstation_id, generation_round, sequence_no, video_file_path) "
                    "VALUES ('T1', 'creative_001', 'A', 'WS_A', ?, ?, ?)",
                    (r, s, f"/out/T1_{r}_{s}.mp4"),
                )
        # T2 partial: 5 candidates
        for s in range(1, 6):
            conn.execute(
                "INSERT INTO task_results (task_id, creative_id, segment_id, "
                "workstation_id, generation_round, sequence_no, video_file_path) "
                "VALUES ('T2', 'creative_001', 'B', 'WS_A', 1, ?, ?)",
                (s, f"/out/T2_{s}.mp4"),
            )
        # T4 partial: 4 candidates
        for s in range(1, 5):
            conn.execute(
                "INSERT INTO task_results (task_id, creative_id, segment_id, "
                "workstation_id, generation_round, sequence_no, video_file_path) "
                "VALUES ('T4', 'creative_002', 'B', 'WS_B', 1, ?, ?)",
                (s, f"/out/T4_{s}.mp4"),
            )
        conn.execute(
            "INSERT INTO workstations (id, account_label, browser_profile_path, "
            "daily_task_limit, status, today_success_count, today_failure_count) "
            "VALUES ('WS_A', 'a', '/p', 20, 'healthy', 1, 1), "
            "('WS_B', 'b', '/p', 20, 'manual_check', 0, 1)"
        )
        conn.execute(
            "INSERT INTO error_logs (task_id, workstation_id, generation_round, "
            "error_type, error_message, screenshot_path) "
            "VALUES ('T3', 'WS_B', 1, 'unusual_activity', 'blocked', '/snap.png')"
        )
    finally:
        conn.close()


def test_report_counts_and_partial(db_path: Path, output_root: Path) -> None:
    _seed_mixed_tasks(db_path)
    out = generate_daily_report(
        db_path=db_path, output_root=output_root, report_date=date.today().isoformat()
    )
    text = out.read_text(encoding="utf-8")
    assert "总任务数（按 Segment 计）：5" in text
    assert "成功任务数（downloaded_count >= target_count）：1" in text
    assert "失败任务数（`failed`）：1" in text
    assert "下载失败任务数（`download_failed`）：2" in text
    assert "「未达标但有产出」任务数" in text
    # Partial-with-output should include T2 (5/8 failed) and T4 (4/8 download_failed) but not T3 (0/8)
    section = text.split("## 未达标但有产出", 1)[1].split("## ", 1)[0]
    assert "T2 |" in section
    assert "T4 |" in section
    assert "T3 |" not in section  # 0/8 download_failed -> not partial-with-output


def test_report_creative_aggregate_view(db_path: Path, output_root: Path) -> None:
    _seed_mixed_tasks(db_path)
    out = generate_daily_report(
        db_path=db_path, output_root=output_root, report_date=date.today().isoformat()
    )
    text = out.read_text(encoding="utf-8")
    # Each creative_id appears
    assert "creative_001" in text
    assert "creative_002" in text
    assert "creative_003" in text
    # Per-segment progress reflected
    assert "A: 8/8" in text
    assert "B: 5/8" in text


def test_report_path_is_sibling_of_sku_dir(db_path: Path, output_root: Path) -> None:
    _seed_mixed_tasks(db_path)
    out = generate_daily_report(
        db_path=db_path, output_root=output_root, report_date="2026-04-28"
    )
    assert out == output_root / "2026-04-28" / "daily_report.md"
    assert out.parent.name == "2026-04-28"


def test_report_includes_partial_download_failed_with_output(db_path: Path, output_root: Path) -> None:
    _seed_mixed_tasks(db_path)
    out = generate_daily_report(
        db_path=db_path, output_root=output_root, report_date=date.today().isoformat()
    )
    text = out.read_text(encoding="utf-8")
    # T4 has 4/8 download_failed -> must appear in partial-with-output section
    section = text.split("## 未达标但有产出", 1)[1].split("## ", 1)[0]
    assert "T4" in section
    assert "T2" in section
