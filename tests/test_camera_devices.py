import json

from qrferry.app import camera_devices


def test_camera_list_uses_probed_opencv_indices(monkeypatch):
    monkeypatch.setattr(camera_devices, "_probe_index", lambda index: index in {1, 3})

    devices = camera_devices.list_camera_devices(max_index=5, probe=True)

    assert [device.index for device in devices] == [1, 3]
    assert [device.name for device in devices] == ["OpenCV Camera 1", "OpenCV Camera 3"]
    assert all(device.available for device in devices)


def test_unprobed_camera_list_does_not_claim_availability():
    devices = camera_devices.list_camera_devices(max_index=2, probe=False)

    assert [device.index for device in devices] == [0, 1]
    assert all(not device.available and not device.probed for device in devices)


def test_probe_available_cameras_only_lists_openable(monkeypatch):
    """真实探测只保留能打开的设备，名称优先用系统友好名，缺名回退 Camera N。"""

    class FakeCap:
        def __init__(self, opened):
            self._opened = opened

        def isOpened(self):
            return self._opened

        def read(self):
            return (self._opened, None)

        def release(self):
            pass

    monkeypatch.setattr(
        camera_devices, "open_camera", lambda index, backend="auto": FakeCap(index in {0, 2}))
    monkeypatch.setattr(
        camera_devices, "query_camera_friendly_names", lambda: ["Cam A"])

    devices = camera_devices.probe_available_cameras(max_index=4)

    assert [device.index for device in devices] == [0, 2]
    assert all(device.available and device.probed for device in devices)
    assert devices[0].label == "0 · Cam A (可用)"
    assert devices[1].label == "2 · Camera 2 (可用)"


def test_probe_available_cameras_empty_when_none_openable(monkeypatch):
    """一个都打不开时返回空列表（兜底项由 UI 层负责）。"""

    class FakeCap:
        def isOpened(self):
            return False

        def release(self):
            pass

    monkeypatch.setattr(camera_devices, "open_camera", lambda index, backend="auto": FakeCap())
    monkeypatch.setattr(camera_devices, "query_camera_friendly_names", list)

    assert camera_devices.probe_available_cameras(max_index=3) == []


def test_probe_filters_devices_that_open_but_never_yield(monkeypatch):
    """isOpened 成功但 read 永不出帧的「假可用」设备必须被过滤，否则选中后静默黑屏。"""

    class FakeCap:
        def isOpened(self):
            return True

        def read(self):
            return (False, None)   # 打开成功但永不出帧（MSMF 协商错误场景）

        def release(self):
            pass

    monkeypatch.setattr(camera_devices, "open_camera", lambda index, backend="auto": FakeCap())
    monkeypatch.setattr(camera_devices, "query_camera_friendly_names", list)

    assert camera_devices.probe_available_cameras(max_index=3) == []


def test_probe_finds_dshow_device_when_msmf_stale(monkeypatch):
    """热插拔：MSMF 枚举陈旧（auto/msmf 命中 isOpened 但 read 失效的陈旧项）时，
    DSHOW 仍能找到真实设备——逐后端独立判定 + 记录 backend 供采集打开复用。"""

    class StaleMsmf:
        def isOpened(self):
            return True

        def read(self):
            return (False, None)   # 陈旧项：能打开但永不出帧

        def release(self):
            pass

    class WorkingDshow:
        def isOpened(self):
            return True

        def read(self):
            return (True, None)

        def release(self):
            pass

    def open_camera(index, backend="auto"):
        if backend == "dshow":
            return WorkingDshow()
        return StaleMsmf()

    monkeypatch.setattr(camera_devices, "open_camera", open_camera)
    monkeypatch.setattr(camera_devices, "query_camera_friendly_names", lambda: ["物理摄像头"])

    devices = camera_devices.probe_available_cameras(max_index=2)

    assert [d.index for d in devices] == [0, 1]
    assert all(d.backend == "dshow" for d in devices), "应记录实际可用的后端 dshow"
    assert devices[0].label == "0 · 物理摄像头 (可用)"


def test_probe_subproc_main_writes_devices(tmp_path, monkeypatch):
    """--probe-cameras 子进程入口：探测结果序列化为 JSON 写到指定路径。"""
    out = tmp_path / "probe.json"

    monkeypatch.setattr(
        camera_devices, "probe_available_cameras",
        lambda max_index=9: [
            camera_devices.CameraDevice(3, "外接摄像头", True, backend="dshow"),
        ])

    camera_devices._probe_cameras_main(["--probe-cameras", "9", str(out)])
    rows = json.loads(out.read_text(encoding="utf-8"))

    assert rows == [{"index": 3, "name": "外接摄像头", "backend": "dshow"}]


def test_is_probe_cameras_request():
    assert camera_devices._is_probe_cameras_request(["run_app.py", "--probe-cameras", "9", "x"])
    assert not camera_devices._is_probe_cameras_request(["run_app.py"])
    assert not camera_devices._is_probe_cameras_request([])
