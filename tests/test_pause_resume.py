import asyncio
import os
import sqlite3

import pytest


async def _prepared(authed, window_index=0):
    windows = (await authed.get("/api/windows")).json()["items"]
    wid = windows[window_index]["id"]
    planned = (await authed.post(f"/api/windows/{wid}/plan")).json()["slices"]
    return wid, planned


async def _replay_all(authed, slices, upto=None):
    out = []
    for sl in slices[:upto]:
        r = await authed.post(f"/api/slices/{sl['id']}/replay")
        assert r.status_code == 200, r.text
        out.append(r.json()["slice"])
    return out


@pytest.mark.asyncio
async def test_pause_resume_lands_new_offset_and_keeps_loaded_rows(authed):
    wid, slices = await _prepared(authed)
    first = await _replay_all(authed, slices, upto=1)
    assert first[0]["status"] == "已落库"
    first_rows = first[0]["row_count"]

    paused = await authed.post(f"/api/windows/{wid}/pause")
    assert paused.status_code == 200
    state = paused.json()["state"]
    assert state["state"] == "已暂停"
    assert state["policy"]["route"] == "A"
    assert "旁路位点账册" in state["policy"]["route_name"]
    assert "未选" in state["policy"]["route_alternative"]
    assert state["policy"]["offset_no"] == 2

    resumed = await authed.post(
        f"/api/windows/{wid}/resume",
        json={"version": state["resume"]["expected_version"], "offset_no": 2},
    )
    assert resumed.status_code == 200, resumed.text
    body = resumed.json()["state"]
    cp = body["resume"]["last_checkpoint"]
    assert cp["decision"] == "有效"
    assert cp["offset_no"] == 2
    assert body["state"] == "运行中"
    # 偏移号与目录同亮新位点：偏移 2 对应的下一片即目录第 2 片
    assert body["policy"]["offset_no"] == 2
    assert body["resume"]["expected_offset"] == 2
    next_row = [d for d in body["directory"] if d["is_next"]]
    assert len(next_row) == 1 and next_row[0]["ordinal"] == 2

    # 已成功片未被覆盖重写
    slices_now = (await authed.get(f"/api/windows/{wid}/slices")).json()["items"]
    assert slices_now[0]["row_count"] == first_rows
    assert slices_now[0]["status"] == "已落库"


@pytest.mark.asyncio
async def test_stale_checkpoint_rejected_and_old_kept_in_ledger(authed):
    wid, slices = await _prepared(authed)
    await _replay_all(authed, slices, upto=1)
    await authed.post(f"/api/windows/{wid}/pause")
    state = (await authed.get(f"/api/windows/{wid}/resume-state")).json()
    v = state["resume"]["expected_version"]
    ok = await authed.post(f"/api/windows/{wid}/resume", json={"version": v, "offset_no": 2})
    assert ok.status_code == 200

    stale = await authed.post(f"/api/windows/{wid}/resume", json={"version": v, "offset_no": 2})
    assert stale.status_code == 409
    payload = stale.json()
    assert payload["reason_code"] == "STALE_CHECKPOINT"
    assert payload["current_offset"] == 2

    state = (await authed.get(f"/api/windows/{wid}/resume-state")).json()
    rejected = [c for c in state["checkpoints"] if c["decision"] == "拒签"]
    assert any(c["reason_code"] == "STALE_CHECKPOINT" for c in rejected)
    accepted = [c for c in state["checkpoints"] if c["decision"] == "有效"]
    # 暂停快照 + 一次续灌位点；旧有效位点仍留在账册可核
    assert len(accepted) == 2
    assert len({c["version"] for c in accepted}) == len(accepted)  # 每版本至多一条有效


@pytest.mark.asyncio
async def test_skip_gap_and_overwrite_rejected(authed):
    wid, slices = await _prepared(authed)
    await _replay_all(authed, slices, upto=1)
    state = (await authed.get(f"/api/windows/{wid}/resume-state")).json()
    v = state["resume"]["expected_version"]

    gap = await authed.post(f"/api/windows/{wid}/resume", json={"version": v, "offset_no": 3})
    assert gap.status_code == 409
    assert gap.json()["reason_code"] == "SKIP_GAP"

    back = await authed.post(f"/api/windows/{wid}/resume", json={"version": v, "offset_no": 1})
    assert back.status_code == 409
    assert back.json()["reason_code"] == "OVERWRITE_LOADED"

    # 位点与进度拧了：偏移对但下一片号不对
    mismatch = await authed.post(
        f"/api/windows/{wid}/resume", json={"version": v, "offset_no": 2, "next_slice_id": 99999}
    )
    assert mismatch.status_code == 409
    assert mismatch.json()["reason_code"] == "MISMATCH_PROGRESS"


@pytest.mark.asyncio
async def test_concurrent_resume_single_ledger_row(authed):
    wid, slices = await _prepared(authed)
    await _replay_all(authed, slices, upto=1)
    await authed.post(f"/api/windows/{wid}/pause")
    state = (await authed.get(f"/api/windows/{wid}/resume-state")).json()
    v = state["resume"]["expected_version"]

    payload = {"version": v, "offset_no": 2}
    results = await asyncio.gather(
        authed.post(f"/api/windows/{wid}/resume", json=payload),
        authed.post(f"/api/windows/{wid}/resume", json=payload),
        return_exceptions=True,
    )
    codes = sorted(r.status_code for r in results)
    # 缺锁会双写位点行；有锁则至多一条有效
    assert codes.count(200) == 1
    assert all(c in (200, 409) for c in codes)

    state = (await authed.get(f"/api/windows/{wid}/resume-state")).json()
    accepted = [c for c in state["checkpoints"] if c["decision"] == "有效"]
    assert len({c["version"] for c in accepted}) == len(accepted)


def test_thread_contention_single_accepted_checkpoint(client):
    """真并发：多线程同时往同一窗口提交位点，写锁必须只放一条有效。"""
    import threading

    from app.services import resume as resume_svc
    from app.services.windows import list_slices, plan_slices, replay_slice

    db = os.environ["DATABASE_PATH"]
    plan_slices(1, db)
    slices = list_slices(1, db)
    replay_slice(slices[0]["id"], db)
    resume_svc.pause_window(1, db_path=db)
    version = resume_svc.get_resume_state(1, db)["resume"]["expected_version"]

    outcomes = []
    barrier = threading.Barrier(8)

    def submit():
        barrier.wait()
        try:
            resume_svc.resume_window(1, version=version, offset_no=2, db_path=db)
            outcomes.append("ok")
        except Exception as exc:  # ServiceError 拒签或 sqlite locked
            outcomes.append(getattr(exc, "reason_code", type(exc).__name__))

    threads = [threading.Thread(target=submit) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    assert outcomes.count("ok") == 1, outcomes
    # 竞态输家必须拿到可读拒签，而不是原始 database is locked
    assert set(outcomes) <= {"ok", "STALE_CHECKPOINT", "LOCK_BUSY"}, outcomes
    state = resume_svc.get_resume_state(1, db)
    accepted = [c for c in state["checkpoints"] if c["decision"] == "有效"]
    assert len({c["version"] for c in accepted}) == len(accepted)
    assert len([c for c in accepted if c["source"] == "续灌"]) == 1


@pytest.mark.asyncio
async def test_replay_blocked_while_paused_and_no_gap_jump(authed):
    wid, slices = await _prepared(authed)
    await authed.post(f"/api/windows/{wid}/pause")
    blocked = await authed.post(f"/api/slices/{slices[0]['id']}/replay")
    assert blocked.status_code == 409
    assert blocked.json()["reason_code"] == "WINDOW_PAUSED"

    state = (await authed.get(f"/api/windows/{wid}/resume-state")).json()
    await authed.post(
        f"/api/windows/{wid}/resume",
        json={"version": state["resume"]["expected_version"], "offset_no": 1},
    )
    skip = await authed.post(f"/api/slices/{slices[2]['id']}/replay")
    assert skip.status_code == 409
    assert skip.json()["reason_code"] == "SKIP_GAP"
    ok = await authed.post(f"/api/slices/{slices[0]['id']}/replay")
    assert ok.status_code == 200


@pytest.mark.asyncio
async def test_replan_does_not_overwrite_loaded_slices(authed):
    wid, slices = await _prepared(authed)
    loaded = await _replay_all(authed, slices, upto=1)
    rows_before = loaded[0]["row_count"]

    # 再把窗口拉回可计划态（草稿/已计划）以检验目录再跑
    conn = sqlite3.connect(os.environ["DATABASE_PATH"])
    conn.execute("UPDATE backfill_windows SET status='已计划' WHERE id=?", (wid,))
    conn.execute("DELETE FROM backfill_runtime WHERE window_id=?", (wid,))
    conn.commit()
    conn.close()

    again = await authed.post(f"/api/windows/{wid}/plan")
    assert again.status_code == 200, again.text
    items = again.json()["slices"]
    assert len(items) == 3
    kept = [s for s in items if s["slice_date"] == slices[0]["slice_date"]][0]
    assert kept["status"] == "已落库"
    assert kept["row_count"] == rows_before


@pytest.mark.asyncio
async def test_pause_after_finish_rejected(authed):
    wid, slices = await _prepared(authed)
    await _replay_all(authed, slices)
    r = await authed.post(f"/api/windows/{wid}/pause")
    assert r.status_code == 409
    assert r.json()["reason_code"] == "WINDOW_DONE"
