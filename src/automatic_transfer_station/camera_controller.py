from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Protocol

from .paths import CAMERA_SNAPSHOT_DIR


DEFAULT_CAMERA_SERIAL = "25141247"
DEFAULT_CAMERA_MODEL = "puA2500-14ucMIC"


class CameraError(RuntimeError):
    """Camera operation failed in a user-visible way."""


@dataclass(frozen=True)
class CameraDeviceInfo:
    model_name: str
    serial_number: str
    device_class: str = ""


@dataclass(frozen=True)
class CameraFrame:
    width: int
    height: int
    rgb_bytes: bytes
    metadata: dict[str, object] = field(default_factory=dict)


class CameraHandle(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def start_preview(self) -> None: ...
    def stop_preview(self) -> None: ...
    def grab_rgb(self, timeout_ms: int = 1000) -> CameraFrame: ...
    def set_exposure(self, auto: bool, value_ms: float | None) -> None: ...
    def set_gain(self, auto: bool, value: float | None) -> None: ...


class CameraBackend(Protocol):
    def list_devices(self) -> list[CameraDeviceInfo]: ...
    def open_device(self, serial_number: str) -> CameraHandle: ...


def snapshot_path(snapshot_dir: Path = CAMERA_SNAPSHOT_DIR, *, now: Callable[[], str] | None = None) -> Path:
    if now is None:
        from datetime import datetime

        stamp = datetime.now().strftime("%Y%m%d-%H%M%S")
    else:
        stamp = str(now())
    return Path(snapshot_dir) / f"camera-{stamp}.png"


class BaslerCameraController:
    def __init__(
        self,
        backend: CameraBackend | None = None,
        preferred_serial: str = DEFAULT_CAMERA_SERIAL,
    ):
        self.backend = backend if backend is not None else PypylonBackend()
        self.preferred_serial = str(preferred_serial)
        self._handle: CameraHandle | None = None
        self._device: CameraDeviceInfo | None = None
        self._previewing = False

    @property
    def is_open(self) -> bool:
        return self._handle is not None

    @property
    def is_previewing(self) -> bool:
        return self._previewing

    @property
    def current_device(self) -> CameraDeviceInfo | None:
        return self._device

    def list_devices(self) -> list[CameraDeviceInfo]:
        return self.backend.list_devices()

    def open(self, serial: str | None = None) -> CameraDeviceInfo:
        requested_serial = str(serial).strip() if serial else ""
        devices = self.list_devices()
        if not devices:
            raise CameraError("No Basler camera found. Check USB connection and close pylon Viewer.")

        selected = self._select_device(devices, requested_serial or self.preferred_serial)
        if selected is None:
            if requested_serial:
                raise CameraError(f"Basler camera serial {requested_serial} not found")
            selected = devices[0]

        if self._handle is not None:
            if self._device is not None and self._device.serial_number == selected.serial_number:
                return self._device
            self.close()

        handle = self.backend.open_device(selected.serial_number)
        try:
            handle.open()
        except Exception as exc:  # noqa: BLE001 - normalize backend/pypylon exceptions
            raise CameraError(f"Failed to open Basler camera {selected.serial_number}: {exc}") from exc
        self._handle = handle
        self._device = selected
        self._previewing = False
        return selected

    @staticmethod
    def _select_device(devices: list[CameraDeviceInfo], serial: str) -> CameraDeviceInfo | None:
        for device in devices:
            if device.serial_number == serial:
                return device
        return None

    def close(self) -> None:
        handle = self._handle
        if handle is None:
            return
        try:
            if self._previewing:
                handle.stop_preview()
        finally:
            self._previewing = False
            try:
                handle.close()
            finally:
                self._handle = None
                self._device = None

    def start_preview(self) -> None:
        handle = self._require_handle()
        try:
            handle.start_preview()
        except Exception as exc:  # noqa: BLE001
            raise CameraError(f"Failed to start camera preview: {exc}") from exc
        self._previewing = True

    def stop_preview(self) -> None:
        handle = self._handle
        if handle is None or not self._previewing:
            return
        try:
            handle.stop_preview()
        except Exception as exc:  # noqa: BLE001
            raise CameraError(f"Failed to stop camera preview: {exc}") from exc
        finally:
            self._previewing = False

    def set_exposure(self, auto: bool, value_ms: float | None = None) -> None:
        handle = self._require_handle()
        try:
            handle.set_exposure(bool(auto), None if value_ms is None else float(value_ms))
        except Exception as exc:  # noqa: BLE001
            raise CameraError(f"Failed to set camera exposure: {exc}") from exc

    def set_gain(self, auto: bool, value: float | None = None) -> None:
        handle = self._require_handle()
        try:
            handle.set_gain(bool(auto), None if value is None else float(value))
        except Exception as exc:  # noqa: BLE001
            raise CameraError(f"Failed to set camera gain: {exc}") from exc

    def grab_one(self, timeout_ms: int = 1000) -> CameraFrame:
        handle = self._require_handle()
        try:
            frame = handle.grab_rgb(int(timeout_ms))
        except Exception as exc:  # noqa: BLE001
            raise CameraError(f"Failed to grab camera frame: {exc}") from exc
        if frame is None:
            raise CameraError("Camera returned no frame")
        return frame

    def _require_handle(self) -> CameraHandle:
        if self._handle is None:
            raise CameraError("Basler camera is not connected")
        return self._handle


class PypylonBackend:
    def __init__(self):
        try:
            from pypylon import pylon
        except Exception as exc:  # pragma: no cover - depends on local install
            self._pylon = None
            self._import_error = exc
            self._factory = None
        else:
            self._pylon = pylon
            self._import_error = None
            self._factory = pylon.TlFactory.GetInstance()

    def _require_pypylon(self):
        if self._pylon is None:
            raise CameraError(f"pypylon is not installed or cannot be loaded: {self._import_error}")
        return self._pylon

    def list_devices(self) -> list[CameraDeviceInfo]:
        self._require_pypylon()
        assert self._factory is not None
        return [
            CameraDeviceInfo(
                model_name=device.GetModelName(),
                serial_number=device.GetSerialNumber(),
                device_class=device.GetDeviceClass(),
            )
            for device in self._factory.EnumerateDevices()
        ]

    def open_device(self, serial_number: str) -> CameraHandle:
        pylon = self._require_pypylon()
        assert self._factory is not None
        for device in self._factory.EnumerateDevices():
            if device.GetSerialNumber() == serial_number:
                camera = pylon.InstantCamera(self._factory.CreateDevice(device))
                return PypylonCameraHandle(pylon, camera)
        raise CameraError(f"Basler camera serial {serial_number} not found")


class PypylonCameraHandle:
    def __init__(self, pylon, camera):
        self._pylon = pylon
        self._camera = camera
        self._converter = pylon.ImageFormatConverter()
        self._converter.OutputPixelFormat = pylon.PixelType_RGB8packed
        self._converter.OutputBitAlignment = pylon.OutputBitAlignment_MsbAligned
        self._single_grab_started = False

    def open(self) -> None:
        if not self._camera.IsOpen():
            self._camera.Open()

    def close(self) -> None:
        try:
            if self._camera.IsGrabbing():
                self._camera.StopGrabbing()
        finally:
            if self._camera.IsOpen():
                self._camera.Close()

    def start_preview(self) -> None:
        if not self._camera.IsOpen():
            self._camera.Open()
        if not self._camera.IsGrabbing():
            self._camera.StartGrabbing(self._pylon.GrabStrategy_LatestImageOnly)
            self._single_grab_started = False

    def stop_preview(self) -> None:
        if self._camera.IsGrabbing():
            self._camera.StopGrabbing()
        self._single_grab_started = False

    def grab_rgb(self, timeout_ms: int = 1000) -> CameraFrame:
        if not self._camera.IsOpen():
            self._camera.Open()
        if not self._camera.IsGrabbing():
            self._camera.StartGrabbingMax(1)
            self._single_grab_started = True

        result = self._camera.RetrieveResult(int(timeout_ms), self._pylon.TimeoutHandling_ThrowException)
        try:
            if not result.GrabSucceeded():
                raise CameraError(f"{result.ErrorCode}: {result.ErrorDescription}")
            image = self._converter.Convert(result)
            array = image.GetArray()
            rgb_bytes = bytes(array.tobytes())
            return CameraFrame(
                width=int(result.Width),
                height=int(result.Height),
                rgb_bytes=rgb_bytes,
                metadata={
                    "pixel_type": int(result.PixelType),
                    "serial_number": self._camera.GetDeviceInfo().GetSerialNumber(),
                    "model_name": self._camera.GetDeviceInfo().GetModelName(),
                },
            )
        finally:
            result.Release()
            if self._single_grab_started and self._camera.IsGrabbing():
                self._camera.StopGrabbing()
                self._single_grab_started = False

    def set_exposure(self, auto: bool, value_ms: float | None) -> None:
        self._set_feature("ExposureAuto", "Continuous" if auto else "Off")
        if not auto and value_ms is not None:
            self._set_feature("ExposureTime", float(value_ms) * 1000.0)

    def set_gain(self, auto: bool, value: float | None) -> None:
        self._set_feature("GainAuto", "Continuous" if auto else "Off")
        if not auto and value is not None:
            self._set_feature("Gain", float(value))

    def _set_feature(self, name: str, value) -> None:
        try:
            feature = getattr(self._camera, name)
        except AttributeError as exc:
            raise CameraError(f"Camera feature {name} is unavailable") from exc
        if hasattr(feature, "SetValue"):
            feature.SetValue(value)
            return
        try:
            feature.Value = value
        except Exception as exc:  # noqa: BLE001
            raise CameraError(f"Camera feature {name} rejected value {value!r}") from exc
