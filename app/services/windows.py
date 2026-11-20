from datetime import date, timedelta

from app.db import get_db, write_tx
from app.services.errors import ServiceError
from app.services.resume import progress_of


def list_windows(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT w.*, "
            "(SELECT COUNT(*) FROM backfill_slices s WHERE s.window_id=w.id) AS slice_count, "
            "(SELECT COUNT(*) FROM backfill_slices s WHERE s.window_id=w.id AND s.status='已落库') AS loaded_count, "
            "COALESCE((SELECT r.state FROM backfill_runtime r WHERE r.window_id=w.id), "
            "         CASE WHEN w.status='已完成' THEN '已完成' ELSE '运行中' END) AS run_state, "
            "(SELECT c.offset_no FROM backfill_checkpoints c WHERE c.window_id=w.id "
            "   AND c.decision='有效' ORDER BY c.version DESC LIMIT 1) AS checkpoint_offset, "
            "(SELECT c.version FROM backfill_checkpoints c WHERE c.window_id=w.id "
            "   AND c.decision='有效' ORDER BY c.version DESC LIMIT 1) AS checkpoint_version, "
            "(SELECT COUNT(*) FROM backfill_checkpoints c WHERE c.window_id=w.id "
            "   AND c.decision='拒签') AS rejected_count "
            "FROM backfill_windows w ORDER BY w.id"
        ).fetchall()
        return [dict(r) for r in rows]


def _daterange(start: str, end: str) -> list[str]:
    s = date.fromisoformat(start)
    e = date.fromisoformat(end)
    if e < s:
        raise ServiceError("结束日不能早于开始日")
    out = []
    cur = s
    while cur <= e:
        out.append(cur.isoformat())
        cur += timedelta(days=1)
    return out


def _runtime_state(conn, window_id: int, win) -> str:
    row = conn.execute(
        "SELECT state FROM backfill_runtime WHERE window_id=?", (window_id,)
    ).fetchone()
    if row:
        return row["state"]
    return "已完成" if win["status"] == "已完成" else "运行中"


def plan_slices(window_id: int, db_path: str | None = None) -> dict:
    """目录再跑：已成功片原样保留，不允许借重新计划制造跳空或覆盖。"""
    with write_tx(db_path) as conn:
        win = conn.execute(
            "SELECT * FROM backfill_windows WHERE id=?", (window_id,)
        ).fetchone()
        if not win:
            raise ServiceError("回灌窗口不存在", 404, reason_code="WINDOW_NOT_FOUND")
        if win["status"] == "已完成":
            raise ServiceError("窗口已完成，不可再生成切片", 409, reason_code="WINDOW_DONE")
        if _runtime_state(conn, window_id, win) == "已暂停":
            raise ServiceError("窗口已暂停，续灌前不可重排目录", 409, reason_code="WINDOW_PAUSED")
        days = _daterange(win["start_date"], win["end_date"])
        existing = conn.execute(
            "SELECT * FROM backfill_slices WHERE window_id=?", (window_id,)
        ).fetchall()
        # 只有已成功片进入保留集；待重放片随目录重排重建。
        keep = {r["slice_date"]: r for r in existing if r["status"] == "已落库"}
        orphans = [r["slice_date"] for r in existing if r["slice_date"] not in set(days)]
        if orphans:
            raise ServiceError(
                "目录再跑会甩掉已存在的切片，拒绝跳空",
                409,
                reason_code="DIRECTORY_GAP",
                details={"orphans": orphans},
            )
        # 已成功片原样保留：目录再跑只补缺日，拒绝覆盖重写。
        conn.execute("DELETE FROM backfill_slices WHERE window_id=? AND status='待重放'", (window_id,))
        for d in days:
            if d in keep:
                continue
            conn.execute(
                "INSERT INTO backfill_slices(window_id, slice_date, status, row_count) "
                "VALUES (?,?, '待重放', 0)",
                (window_id, d),
            )
        if not keep:
            conn.execute(
                "UPDATE backfill_windows SET status='已计划' WHERE id=?",
                (window_id,),
            )
        slices = conn.execute(
            "SELECT * FROM backfill_slices WHERE window_id=? ORDER BY slice_date",
            (window_id,),
        ).fetchall()
        return {"window_id": window_id, "slices": [dict(s) for s in slices]}


def list_slices(window_id: int, db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM backfill_slices WHERE window_id=? ORDER BY slice_date",
            (window_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def replay_slice(slice_id: int, db_path: str | None = None) -> dict:
    with write_tx(db_path) as conn:
        sl = conn.execute(
            "SELECT * FROM backfill_slices WHERE id=?", (slice_id,)
        ).fetchone()
        if not sl:
            raise ServiceError("切片不存在", 404, reason_code="SLICE_NOT_FOUND")
        win = conn.execute(
            "SELECT * FROM backfill_windows WHERE id=?", (sl["window_id"],)
        ).fetchone()
        if _runtime_state(conn, sl["window_id"], win) == "已暂停":
            raise ServiceError("窗口已暂停，先续灌再重放", 409, reason_code="WINDOW_PAUSED")
        if sl["status"] == "已落库":
            raise ServiceError("切片已落库", 409, reason_code="ALREADY_LOADED")
        if sl["status"] != "待重放":
            raise ServiceError("仅待重放切片可重放", 409, reason_code="BAD_STATUS")
        ordered = conn.execute(
            "SELECT * FROM backfill_slices WHERE window_id=? ORDER BY slice_date, id",
            (sl["window_id"],),
        ).fetchall()
        progress = progress_of(ordered)
        if sl["id"] != progress["next_slice_id"]:
            raise ServiceError(
                "必须按目录接续重放，不得跳空", 409, reason_code="SKIP_GAP",
                details={
                    "reason_code": "SKIP_GAP",
                    "current_offset": progress["expected_offset"],
                    "next_slice_id": progress["next_slice_id"],
                },
            )
        # deterministic fake row count from date digits
        rows = 100 + int(sl["slice_date"].replace("-", "")) % 97
        conn.execute(
            "UPDATE backfill_slices SET status='已落库', row_count=? WHERE id=?",
            (rows, slice_id),
        )
        conn.execute(
            "UPDATE backfill_windows SET status='执行中' WHERE id=?",
            (sl["window_id"],),
        )
        pending = conn.execute(
            "SELECT COUNT(*) AS c FROM backfill_slices WHERE window_id=? AND status!='已落库'",
            (sl["window_id"],),
        ).fetchone()["c"]
        if pending == 0:
            conn.execute(
                "UPDATE backfill_windows SET status='已完成' WHERE id=?",
                (sl["window_id"],),
            )
            conn.execute(
                "INSERT INTO backfill_runtime(window_id, state, updated_at) "
                "VALUES (?, '已完成', datetime('now')) "
                "ON CONFLICT(window_id) DO UPDATE SET state='已完成', updated_at=datetime('now')",
                (sl["window_id"],),
            )
        row = conn.execute(
            "SELECT * FROM backfill_slices WHERE id=?", (slice_id,)
        ).fetchone()
        return dict(row)


def coverage_report(window_id: int, db_path: str | None = None) -> dict:
    with get_db(db_path) as conn:
        win = conn.execute(
            "SELECT * FROM backfill_windows WHERE id=?", (window_id,)
        ).fetchone()
        if not win:
            raise ServiceError("回灌窗口不存在", 404)
        planned = conn.execute(
            "SELECT COUNT(*) AS c FROM backfill_slices WHERE window_id=?",
            (window_id,),
        ).fetchone()["c"]
        loaded = conn.execute(
            "SELECT COUNT(*) AS c FROM backfill_slices WHERE window_id=? AND status='已落库'",
            (window_id,),
        ).fetchone()["c"]
        missing = planned - loaded
        summary = (
            f"窗口#{window_id} {win['table_name']}：计划{planned}片，已落库{loaded}，缺口{missing}"
        )
        cur = conn.execute(
            "INSERT INTO coverage_reports(window_id, planned, loaded, missing, summary) "
            "VALUES (?,?,?,?,?)",
            (window_id, planned, loaded, missing, summary),
        )
        row = conn.execute(
            "SELECT * FROM coverage_reports WHERE id=?", (cur.lastrowid,)
        ).fetchone()
        return dict(row)


def list_coverage(db_path: str | None = None) -> list[dict]:
    with get_db(db_path) as conn:
        rows = conn.execute(
            "SELECT * FROM coverage_reports ORDER BY id DESC"
        ).fetchall()
        return [dict(r) for r in rows]
