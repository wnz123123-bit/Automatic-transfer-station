from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
import threading
from typing import Callable, Protocol

from .paths import CAMERA_SNAPSHOT_DIR


DEFAULT_CAMERA_SERIAL = "25141247"
DEFAULT_CAMERA_MODEL = "puA2500-14ucMIC"
DEFAULT_CAMERA_GRAB_TIMEOUT_MS = 5000


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
    binning: int | None = 1


@dataclass(frozen=True)
class FeatureRange:
    min: float
    max: float
    inc: float | None = None


@dataclass(frozen=True)
class CameraCapabilities:
    sensor_width: int | None = None
    sensor_height: int | None = None
    width: FeatureRange | None = None
    height: FeatureRange | None = None
    binning: FeatureRange | None = None
    exposure_ms: FeatureRange | None = None
    gain: FeatureRange | None = None
    gamma: FeatureRange | None = None
    sharpness: FeatureRange | None = None


@dataclass(frozen=True)
class CameraAcquisitionSettings:
    width: int
    height: int
    binning: int = 1


class CameraHandle(Protocol):
    def open(self) -> None: ...
    def close(self) -> None: ...
    def start_preview(self) -> None: ...
    def stop_preview(self) -> None: ...
    def grab_rgb(self, timeout_ms: int = DEFAULT_CAMERA_GRAB_TIMEOUT_MS) -> CameraFrame: ...
    def set_exposure(self, auto: bool, value_ms: float | None) -> None: ...
    def set_gain(self, auto: bool, value: float | None) -> None: ...
    def capabilities(self) -> CameraCapabilities: ...
    def apply_acquisition_settings(self, settings: CameraAcquisitionSettings) -> None: ...
    def set_image_adjustment(self, gamma: float | None, sharpness: float | None) -> None: ...


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
        self._io_lock = threading.RLock()

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
        with self._io_lock:
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
        with self._io_lock:
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
        with self._io_lock:
            handle = self._require_handle()
            try:
                handle.start_preview()
            except Exception as exc:  # noqa: BLE001
                raise CameraError(f"Failed to start camera preview: {exc}") from exc
            self._previewing = True

    def stop_preview(self) -> None:
        with self._io_lock:
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
        with self._io_lock:
            handle = self._require_handle()
            try:
                handle.set_exposure(bool(auto), None if value_ms is None else float(value_ms))
            except Exception as exc:  # noqa: BLE001
                raise CameraError(f"Failed to set camera exposure: {exc}") from exc

    def set_gain(self, auto: bool, value: float | None = None) -> None:
        with self._io_lock:
            handle = self._require_handle()
            try:
                handle.set_gain(bool(auto), None if value is None else float(value))
            except Exception as exc:  # noqa: BLE001
                raise CameraError(f"Failed to set camera gain: {exc}") from exc

    def capabilities(self) -> CameraCapabilities:
        with self._io_lock:
            handle = self._require_handle()
            try:
                return handle.capabilities()
            except Exception as exc:  # noqa: BLE001
                raise CameraError(f"Failed to read camera capabilities: {exc}") from exc

    def apply_acquisition_settings(self, settings: CameraAcquisitionSettings) -> None:
        with self._io_lock:
            try:
                self._apply_with_preview_paused(lambda handle: handle.apply_acquisition_settings(settings))
            except Exception as exc:  # noqa: BLE001
                raise CameraError(f"Failed to set camera resolution: {exc}") from exc

    def set_image_adjustment(self, gamma: float | None = None, sharpness: float | None = None) -> None:
        with self._io_lock:
            handle = self._require_handle()
            try:
                handle.set_image_adjustment(gamma=gamma, sharpness=sharpness)
            except Exception as exc:  # noqa: BLE001
                raise CameraError(f"Failed to set camera image adjustment: {exc}") from exc

    def grab_one(self, timeout_ms: int = DEFAULT_CAMERA_GRAB_TIMEOUT_MS) -> CameraFrame:
        with self._io_lock:
            handle = self._require_handle()
            try:
                frame = handle.grab_rgb(int(timeout_ms))
            except Exception as exc:  # noqa: BLE001
                raise CameraError(f"Failed to grab camera frame: {exc}") from exc
            if frame is None:
                raise CameraError("Camera returned no frame")
            return frame

    def grab_reference(self, timeout_ms: int = DEFAULT_CAMERA_GRAB_TIMEOUT_MS) -> CameraFrame:
        """Acquire a fresh stationary reference, flushing preview's queued image.

        Uses the same camera handle/lock as preview. Stopping acquisition before
        the single grab avoids attributing a buffered pre-motion frame to a new
        stage pose. Acquisition resolution is unchanged.
        """
        with self._io_lock:
            handle = self._require_handle()
            was_previewing = self._previewing
            failure = None
            try:
                handle.stop_preview()
                self._previewing = False
                frame = handle.grab_rgb(int(timeout_ms))
                if frame is None:
                    raise CameraError("Camera returned no reference frame")
                return frame
            except Exception as exc:
                failure = CameraError(f"Failed to grab camera reference: {exc}")
                raise failure from exc
            finally:
                if was_previewing and self._handle is handle:
                    try:
                        handle.start_preview()
                    except Exception as restore_error:
                        self._previewing = False
                        detail = f"{failure}; " if failure is not None else ""
                        raise CameraError(f"{detail}Failed to restore preview: {restore_error}") from (failure or restore_error)
                    else:
                        self._previewing = True

    def grab_one_with_settings(
        self,
        snap_settings: CameraAcquisitionSettings,
        restore_settings: CameraAcquisitionSettings | None = None,
        timeout_ms: int = DEFAULT_CAMERA_GRAB_TIMEOUT_MS,
    ) -> CameraFrame:
        with self._io_lock:
            handle = self._require_handle()
            try:
                handle.apply_acquisition_settings(snap_settings)
                frame = handle.grab_rgb(int(timeout_ms))
            except Exception as exc:  # noqa: BLE001
                raise CameraError(f"Failed to grab camera frame: {exc}") from exc
            finally:
                if restore_settings is not None:
                    try:
                        handle.apply_acquisition_settings(restore_settings)
                    except Exception:
                        pass
            if frame is None:
                raise CameraError("Camera returned no frame")
            return frame

    def _require_handle(self) -> CameraHandle:
        if self._handle is None:
            raise CameraError("Basler camera is not connected")
        return self._handle

    def _apply_with_preview_paused(self, action: Callable[[CameraHandle], object]) -> object:
        handle = self._require_handle()
        was_previewing = self._previewing
        if was_previewing:
            handle.stop_preview()
            self._previewing = False
        try:
            return action(handle)
        finally:
            if was_previewing and self._handle is handle:
                handle.start_preview()
                self._previewing = True


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
        self._binning: int | None = None
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
            self._prepare_free_run_acquisition()
            self._camera.StartGrabbing(self._pylon.GrabStrategy_LatestImageOnly)
            self._single_grab_started = False

    def stop_preview(self) -> None:
        if self._camera.IsGrabbing():
            self._camera.StopGrabbing()
        self._single_grab_started = False

    def grab_rgb(self, timeout_ms: int = DEFAULT_CAMERA_GRAB_TIMEOUT_MS) -> CameraFrame:
        if not self._camera.IsOpen():
            self._camera.Open()
        if not self._camera.IsGrabbing():
            self._prepare_free_run_acquisition()
            self._camera.StartGrabbingMax(1)
            self._single_grab_started = True

        result = None
        try:
            result = self._camera.RetrieveResult(int(timeout_ms), self._pylon.TimeoutHandling_ThrowException)
            if not result.GrabSucceeded():
                raise CameraError(f"{result.ErrorCode}: {result.ErrorDescription}")
            image = self._converter.Convert(result)
            array = image.GetArray()
            rgb_bytes = bytes(array.tobytes())
            return CameraFrame(
                width=int(result.Width),
                height=int(result.Height),
                rgb_bytes=rgb_bytes,
                binning=self._binning,
                metadata={
                    "pixel_type": int(result.PixelType),
                    "serial_number": self._camera.GetDeviceInfo().GetSerialNumber(),
                    "model_name": self._camera.GetDeviceInfo().GetModelName(),
                },
            )
        finally:
            if result is not None:
                result.Release()
            if self._single_grab_started:
                try:
                    if self._camera.IsGrabbing():
                        self._camera.StopGrabbing()
                finally:
                    self._single_grab_started = False

    def set_exposure(self, auto: bool, value_ms: float | None) -> None:
        self._set_feature("ExposureAuto", "Continuous" if auto else "Off")
        if not auto and value_ms is not None:
            self._set_feature("ExposureTime", float(value_ms) * 1000.0)

    def set_gain(self, auto: bool, value: float | None) -> None:
        self._set_feature("GainAuto", "Continuous" if auto else "Off")
        if not auto and value is not None:
            self._set_feature("Gain", float(value))

    def capabilities(self) -> CameraCapabilities:
        return CameraCapabilities(
            sensor_width=self._int_value("SensorWidth"),
            sensor_height=self._int_value("SensorHeight"),
            width=self._range("Width"),
            height=self._range("Height"),
            binning=self._range("BinningHorizontal"),
            exposure_ms=self._range("ExposureTime", scale=0.001),
            gain=self._range("Gain"),
            gamma=self._range("Gamma"),
            sharpness=self._range("SharpnessEnhancement"),
        )

    def apply_acquisition_settings(self, settings: CameraAcquisitionSettings) -> None:
        binning = max(1, int(settings.binning))
        for name in ("BinningHorizontal", "BinningVertical"):
            self._set_feature_if_available(name, binning)
        self._set_feature_if_available("Width", int(settings.width))
        self._set_feature_if_available("Height", int(settings.height))

    def set_image_adjustment(self, gamma: float | None = None, sharpness: float | None = None) -> None:
        if gamma is not None:
            self._set_feature_if_available("Gamma", float(gamma))
        if sharpness is not None:
            self._set_feature_if_available("SharpnessEnhancement", float(sharpness))

    def _prepare_free_run_acquisition(self) -> None:
        # Read the applied mode once per acquisition. Some cameras ignore binning
        # writes or crop the ROI, so image width alone cannot establish scale.
        horizontal = self._binning_factor("BinningHorizontal")
        vertical = self._binning_factor("BinningVertical")
        self._binning = horizontal if horizontal == vertical else None
        self._set_feature_if_present("AcquisitionMode", "Continuous")
        for selector in self._feature_symbolics("TriggerSelector", fallback=("FrameStart",)):
            self._set_feature_if_present("TriggerSelector", selector)
            self._set_feature_if_present("TriggerMode", "Off")

    def _set_feature(self, name: str, value) -> None:
        try:
            feature = getattr(self._camera, name)
        except AttributeError as exc:
            raise CameraError(f"Camera feature {name} is unavailable") from exc
        if hasattr(feature, "SetValue"):
            try:
                feature.SetValue(value)
            except Exception as exc:  # noqa: BLE001
                raise CameraError(f"Camera feature {name} rejected value {value!r}") from exc
            return
        try:
            feature.Value = value
        except Exception as exc:  # noqa: BLE001
            raise CameraError(f"Camera feature {name} rejected value {value!r}") from exc

    def _set_feature_if_available(self, name: str, value) -> None:
        try:
            self._set_feature(name, value)
        except CameraError:
            return

    def _set_feature_if_present(self, name: str, value) -> bool:
        try:
            getattr(self._camera, name)
        except Exception:
            return False
        self._set_feature(name, value)
        return True

    def _feature_symbolics(self, name: str, fallback: tuple[str, ...] = ()) -> tuple[str, ...]:
        try:
            feature = getattr(self._camera, name)
        except Exception:
            return tuple(fallback)
        for attr in ("GetSymbolics",):
            getter = getattr(feature, attr, None)
            if getter is None:
                continue
            try:
                values = getter()
            except Exception:
                continue
            if values:
                return tuple(str(value) for value in values)
        try:
            values = feature.Symbolics
        except Exception:
            return tuple(fallback)
        return tuple(str(value) for value in values) if values else tuple(fallback)

    def _range(self, name: str, scale: float = 1.0) -> FeatureRange | None:
        try:
            feature = getattr(self._camera, name)
        except Exception:
            return None
        try:
            minimum = feature.GetMin()
            maximum = feature.GetMax()
        except Exception:
            return None
        try:
            inc = feature.GetInc()
        except Exception:
            inc = None
        return FeatureRange(float(minimum) * scale, float(maximum) * scale, None if inc is None else float(inc) * scale)

    def _binning_factor(self, name: str) -> int | None:
        value = self._int_value(name)
        if value is not None:
            return value if value > 0 else None
        # A camera with no such feature has fixed 1x sampling on this axis.
        # A feature that exists but could not be read is an unknown mode.
        try:
            if self._camera.GetNodeMap().GetNode(name) is None:
                return 1
        except Exception:
            pass
        return None

    def _int_value(self, name: str) -> int | None:
        try:
            feature = getattr(self._camera, name)
        except Exception:
            return None
        try:
            value = feature.GetValue()
        except Exception:
            try:
                value = feature.Value
            except Exception:
                return None
        try:
            return int(value)
        except (TypeError, ValueError):
            return None
