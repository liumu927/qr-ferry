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
