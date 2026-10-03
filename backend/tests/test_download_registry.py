from backend.core.download_registry import DownloadRegistry


def test_create_update_snapshot_lifecycle():
    r = DownloadRegistry(history=2)
    a = r.create(task_id=1, taskname="T", filename="a.mp4", dest_path="/d/a.mp4", total=100)
    b = r.create(task_id=1, taskname="T", filename="b.mp4", dest_path="/d/b.mp4", total=0)
    r.update(a, done=50, speed=12.5, status="downloading")
    by_id = {j["id"]: j for j in r.snapshot()}
    assert by_id[a]["done"] == 50 and by_id[a]["status"] == "downloading"
    assert by_id[a]["speed"] == 12.5 and by_id[b]["status"] == "queued"

    r.update(a, done=100, status="done")
    snap = r.snapshot()
    ids = [j["id"] for j in snap]
    # 进行中(b)排在历史(a)之前
    assert ids.index(b) < ids.index(a)
    assert next(j for j in snap if j["id"] == a)["status"] == "done"


def test_history_is_bounded():
    r = DownloadRegistry(history=2)
    ids = [r.create(task_id=None, taskname="T", filename=f"{i}", dest_path="/d", total=1) for i in range(5)]
    for i in ids:
        r.update(i, status="done")
    done_ids = [j["id"] for j in r.snapshot()]
    assert len(done_ids) == 2 and ids[-1] in done_ids and ids[0] not in done_ids


def test_update_unknown_id_is_noop():
    r = DownloadRegistry()
    r.update("nope", done=5, status="done")  # 不抛异常
    assert r.snapshot() == []


def test_snapshot_marks_builtin_source():
    from backend.core.download_registry import registry
    jid = registry.create(task_id=None, taskname="t", filename="f.mkv", dest_path="/d/f.mkv", total=10)
    row = next(j for j in registry.snapshot() if j["id"] == jid)
    assert row["source"] == "builtin"
    registry.remove(jid)


def test_stop_sets_cancel_and_remove_drops():
    from backend.core.download_registry import registry
    jid = registry.create(task_id=None, taskname="t", filename="g.mkv", dest_path="/d/g.mkv", total=10)
    assert registry.cancel_requested(jid) is False
    assert registry.stop(jid) is True
    assert registry.cancel_requested(jid) is True
    assert registry.remove(jid) is True
    assert all(j["id"] != jid for j in registry.snapshot())
    assert registry.cancel_requested(jid) is False  # 清理后无残留
