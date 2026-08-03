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

        def release(self):
            pass

    monkeypatch.setattr(
        camera_devices, "open_camera", lambda index: FakeCap(index in {0, 2}))
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

    monkeypatch.setattr(camera_devices, "open_camera", lambda index: FakeCap())
    monkeypatch.setattr(camera_devices, "query_camera_friendly_names", list)

    assert camera_devices.probe_available_cameras(max_index=3) == []
