"""暂停 / 续灌与位点账册。

落库路线：**路线 A（旁路位点账册）**。
- 位点只写入独立的 `backfill_checkpoints` 账册，不改写 `backfill_slices`；
- 位点提交在同一 `BEGIN IMMEDIATE` 事务里先原子记下现行偏移，再判定提交是否有效；
- 判为无效的旧位点写一条 `拒签` 流水后拒掉，已成功片一行不动；
- 旧位点（历史有效位点）仍保留在账册中可查、可复核，但不能再当新位点灌。

**不实现路线 B**（回滚半截重写行再挂新偏移）。两条路线不并挂。
"""

import sqlite3

from app.db import LockBusy, write_tx
from app.services.errors import ServiceError

ROUTE = "A · 旁路位点账册（原子记现行偏移后拒旧位点）"
ROUTE_ALTERNATIVE = "B · 整笔回滚半截重写行再挂新偏移（未选，不并挂）"
ROUTE_NOTE = "选定路线 A：旧位点留在账册可核、仍可复核，但不能再当新位点灌；已成功片不重写。"
POLICY_LINES = [
    "暂停落在片边界，不回滚已落库切片",
    "续灌按现行偏移接续，跳空与回退一律拒签",
    "位点提交走 BEGIN IMMEDIATE 单写锁，账册每版本至多一条有效位点",
]

REJECT_REASONS = {
    "STALE_CHECKPOINT": "旧位点：该版本已有有效位点或版本已过期",
    "VERSION_AHEAD": "位点版本超前，现行版本不允许",
    "SKIP_GAP": "位点跳过空档，目录不允许跳空续灌",
    "OVERWRITE_LOADED": "位点回退到已成功片，拒绝覆盖重写",
    "MISMATCH_PROGRESS": "位点与进度拧了，偏移号与下一片对不上",
}

_LOADED = "已落库"


def _short_circuit(code: str, current_offset: int, **details) -> ServiceError:
    payload = {"reason_code": code, "reason": REJECT_REASONS[code], "current_offset": current_offset}
    payload.update(details)
    return ServiceError(REJECT_REASONS[code], 409, reason_code=code, details=payload)


def _ordered_slices(conn, window_id: int) -> list:
    return conn.execute(
        "SELECT * FROM backfill_slices WHERE window_id=? ORDER BY slice_date, id",
        (window_id,),
    ).fetchall()


def progress_of(slices) -> dict:
    """按目录顺序算现行进度。偏移号 = 1 基的「下一片」位置，全部落库时为 N+1。"""
    total = len(slices)
    contiguous = 0
    for sl in slices:
        if sl["status"] != _LOADED:
            break
        contiguous += 1
    loaded = sum(1 for sl in slices if sl["status"] == _LOADED)
    finished = total > 0 and contiguous == total
    expected_offset = total + 1 if finished else contiguous + 1
    next_slice = None
    if not finished:
        next_slice = slices[contiguous]
    # 空档：首个未落库片之后仍有已落库片，说明目录曾经跳空
    gaps = [
        {"ordinal": i + 1, "slice_id": sl["id"], "slice_date": sl["slice_date"]}
        for i, sl in enumerate(slices)
        if i > contiguous and sl["status"] == _LOADED
    ]
    directory = []
    for i, sl in enumerate(slices):
        ordinal = i + 1
        directory.append(
            {
                "ordinal": ordinal,
                "slice_id": sl["id"],
                "slice_date": sl["slice_date"],
                "status": sl["status"],
                "row_count": sl["row_count"],
                "is_next": ordinal == expected_offset and not finished,
                "is_loaded": sl["status"] == _LOADED,
            }
        )
    return {
        "planned": total,
        "loaded": loaded,
        "contiguous_loaded": contiguous,
        "missing": total - loaded,
        "finished": finished,
        "expected_offset": expected_offset,
        "next_slice_id": next_slice["id"] if next_slice else None,
        "next_slice_date": next_slice["slice_date"] if next_slice else None,
        "gaps": gaps,
        "directory": directory,
    }


def _window(conn, window_id: int):
    win = conn.execute("SELECT * FROM backfill_windows WHERE id=?", (window_id,)).fetchone()
    if not win:
        raise ServiceError("回灌窗口不存在", 404, reason_code="WINDOW_NOT_FOUND")
    return win


def _runtime(conn, window_id: int):
    return conn.execute(
        "SELECT * FROM backfill_runtime WHERE window_id=?", (window_id,)
    ).fetchone()


def _state(win, runtime) -> str:
    if runtime:
        return runtime["state"]
    return "已完成" if win["status"] == "已完成" else "运行中"


def _accepted_checkpoints(conn, window_id: int):
    return conn.execute(
        "SELECT * FROM backfill_checkpoints WHERE window_id=? AND decision='有效' "
        "ORDER BY version",
        (window_id,),
    ).fetchall()


def _expected_version(accepted) -> int:
    return (accepted[-1]["version"] + 1) if accepted else 1


def _record_reject(conn, window_id, version, offset_no, current_offset, code, source, actor, next_slice_id=None):
    conn.execute(
        "INSERT INTO backfill_checkpoints"
        "(window_id, version, offset_no, current_offset, next_slice_id, decision, "
        " reason_code, reason, source, actor) "
        "VALUES (?,?,?,?,?,'拒签',?,?,?,?)",
        (
            window_id,
            version,
            offset_no,
            current_offset,
            next_slice_id,
            code,
            REJECT_REASONS[code],
            source,
            actor,
        ),
    )


def get_resume_state(window_id: int, db_path: str | None = None) -> dict:
    from app.db import get_db

    with get_db(db_path) as conn:
        win = _window(conn, window_id)
        slices = _ordered_slices(conn, window_id)
        runtime = _runtime(conn, window_id)
        accepted = _accepted_checkpoints(conn, window_id)
        progress = progress_of(slices)
        rows = conn.execute(
            "SELECT * FROM backfill_checkpoints WHERE window_id=? ORDER BY id DESC",
            (window_id,),
        ).fetchall()
        checkpoints = [dict(r) for r in rows]
        last = dict(accepted[-1]) if accepted else None
        rejected = [c for c in checkpoints if c["decision"] == "拒签"]
        return {
            "window_id": window_id,
            "table_name": win["table_name"],
            "window_status": win["status"],
            "state": _state(win, runtime),
            "paused_at": runtime["paused_at"] if runtime else None,
            "resume": {
                "expected_version": _expected_version(accepted),
                "expected_offset": progress["expected_offset"],
                "next_slice_id": progress["next_slice_id"],
                "next_slice_date": progress["next_slice_date"],
                "finished": progress["finished"],
                "last_checkpoint": last,
            },
            "policy": {
                "route": "A",
                "route_name": ROUTE,
                "route_alternative": ROUTE_ALTERNATIVE,
                "route_note": ROUTE_NOTE,
                "offset_no": progress["expected_offset"],
                "next_slice_id": progress["next_slice_id"],
                "next_slice_date": progress["next_slice_date"],
                "finished": progress["finished"],
                "lines": POLICY_LINES,
            },
            "checkpoints": checkpoints,
            "rejected_count": len(rejected),
            "coverage": {
                "planned": progress["planned"],
                "loaded": progress["loaded"],
                "contiguous_loaded": progress["contiguous_loaded"],
                "missing": progress["missing"],
                "gaps": progress["gaps"],
            },
            "directory": progress["directory"],
        }


def list_checkpoints(window_id: int, db_path: str | None = None) -> list[dict]:
    from app.db import get_db

    with get_db(db_path) as conn:
        _window(conn, window_id)
        rows = conn.execute(
            "SELECT * FROM backfill_checkpoints WHERE window_id=? ORDER BY id DESC",
            (window_id,),
        ).fetchall()
        return [dict(r) for r in rows]


def pause_window(window_id: int, actor: str = "值班", db_path: str | None = None) -> dict:
    try:
        return _pause_window(window_id, actor, db_path)
    except LockBusy:
        raise ServiceError(
            "另一名值班正在写位点，请稍后重试", 409, reason_code="LOCK_BUSY"
        )


def _pause_window(window_id: int, actor: str, db_path: str | None) -> dict:
    with write_tx(db_path) as conn:
        win = _window(conn, window_id)
        if _state(win, _runtime(conn, window_id)) == "已完成" or win["status"] == "已完成":
            raise ServiceError("窗口已完成，无需暂停", 409, reason_code="WINDOW_DONE")
        if not _ordered_slices(conn, window_id):
            raise ServiceError("先生成切片计划再暂停", 409, reason_code="NO_SLICES")
        conn.execute(
            "INSERT INTO backfill_runtime(window_id, state, paused_at, updated_at) "
            "VALUES (?, '已暂停', datetime('now'), datetime('now')) "
            "ON CONFLICT(window_id) DO UPDATE SET state='已暂停', paused_at=datetime('now'), "
            "updated_at=datetime('now')",
            (window_id,),
        )
        # 暂停也是一次位点快照：账册记下现行偏移，续灌按它复核。
        accepted = _accepted_checkpoints(conn, window_id)
        progress = progress_of(_ordered_slices(conn, window_id))
        conn.execute(
            "INSERT INTO backfill_checkpoints"
            "(window_id, version, offset_no, current_offset, next_slice_id, decision, "
            " reason_code, reason, source, actor) "
            "VALUES (?,?,?,?,?,'有效','OK','暂停快照·现行偏移',?,?)",
            (
                window_id, _expected_version(accepted), progress["expected_offset"],
                progress["expected_offset"], progress["next_slice_id"], "暂停", actor,
            ),
        )
    return get_resume_state(window_id, db_path)


def resume_window(
    window_id: int,
    *,
    version: int | None = None,
    offset_no: int | None = None,
    next_slice_id: int | None = None,
    source: str = "续灌",
    actor: str = "值班",
    db_path: str | None = None,
) -> dict:
    """按路线 A 落位点：单写锁内先记现行偏移，再判定提交是否有效。

    拒签也落账册：拒签流水先随写事务提交，再把 409 抛给调用方，
    这样「旁路位点账册原子记下现行偏移后拒旧位点」在库内可核。
    """
    try:
        return _resume_under_lock(
            window_id,
            version=version,
            offset_no=offset_no,
            next_slice_id=next_slice_id,
            source=source,
            actor=actor,
            db_path=db_path,
        )
    except LockBusy:
        raise ServiceError(
            "另一名值班正在写位点，请稍后重试", 409, reason_code="LOCK_BUSY"
        )


def _resume_under_lock(
    window_id: int,
    *,
    version: int | None,
    offset_no: int | None,
    next_slice_id: int | None,
    source: str,
    actor: str,
    db_path: str | None,
) -> dict:
    rejection: ServiceError | None = None
    with write_tx(db_path) as conn:
        win = _window(conn, window_id)
        slices = _ordered_slices(conn, window_id)
        if not slices:
            raise ServiceError("先生成切片计划再续灌", 409, reason_code="NO_SLICES")
        runtime = _runtime(conn, window_id)
        state = _state(win, runtime)
        accepted = _accepted_checkpoints(conn, window_id)
        progress = progress_of(slices)
        current_offset = progress["expected_offset"]
        expected_version = _expected_version(accepted)

        if state == "已完成" or progress["finished"]:
            raise ServiceError("窗口已完成，无需续灌", 409, reason_code="WINDOW_DONE")

        # 1) 版本判定：旧位点 / 超前位点拒签，并在账册记下现行偏移。
        if version is not None and version != expected_version:
            code = "STALE_CHECKPOINT" if version < expected_version else "VERSION_AHEAD"
            _record_reject(
                conn, window_id, version, offset_no if offset_no is not None else 0,
                current_offset, code, source, actor,
                next_slice_id=progress["next_slice_id"],
            )
            rejection = _short_circuit(code, current_offset, expected_version=expected_version)

        # 2) 偏移判定：跳空 / 回退覆盖已成功片。
        elif offset_no is not None and offset_no != current_offset:
            code = "SKIP_GAP" if offset_no > current_offset else "OVERWRITE_LOADED"
            _record_reject(
                conn, window_id, expected_version, offset_no, current_offset, code, source, actor,
                next_slice_id=progress["next_slice_id"],
            )
            rejection = _short_circuit(
                code, current_offset, expected_version=expected_version,
                next_slice_id=progress["next_slice_id"],
            )

        # 3) 显式给了下一片却对不上进度 → 位点与进度拧了。
        elif next_slice_id is not None and next_slice_id != progress["next_slice_id"]:
            _record_reject(
                conn, window_id, expected_version, current_offset, current_offset,
                "MISMATCH_PROGRESS", source, actor,
                next_slice_id=progress["next_slice_id"],
            )
            rejection = _short_circuit(
                "MISMATCH_PROGRESS", current_offset,
                expected_version=expected_version, next_slice_id=progress["next_slice_id"],
            )

        if rejection is None:
            # 4) 有效位点：与状态切换同一事务提交。
            try:
                conn.execute(
                    "INSERT INTO backfill_checkpoints"
                    "(window_id, version, offset_no, current_offset, next_slice_id, decision, "
                    " reason_code, reason, source, actor) "
                    "VALUES (?,?,?,?,?,'有效','OK','按现行偏移接续',?,?)",
                    (
                        window_id, expected_version, current_offset, current_offset,
                        progress["next_slice_id"], source, actor,
                    ),
                )
            except sqlite3.IntegrityError:
                # 唯一索引兜底：同版本已有有效位点，自己不落第二行。
                rejection = _short_circuit(
                    "STALE_CHECKPOINT", current_offset, expected_version=expected_version
                )
            else:
                conn.execute(
                    "INSERT INTO backfill_runtime(window_id, state, paused_at, updated_at) "
                    "VALUES (?, '运行中', NULL, datetime('now')) "
                    "ON CONFLICT(window_id) DO UPDATE SET state='运行中', updated_at=datetime('now')",
                    (window_id,),
                )
                if win["status"] == "草稿" or win["status"] == "已计划":
                    conn.execute(
                        "UPDATE backfill_windows SET status='执行中' WHERE id=?", (window_id,)
                    )
    # 事务已提交（拒签流水也落了库）后再把拒签抛出去。
    if rejection is not None:
        raise rejection
    return get_resume_state(window_id, db_path)
