#!/usr/bin/env python3
"""Deterministic reference model for the Micro V8 runtime state machine.

Purpose
-------
This module mirrors the simulator firmware's state and three independent
timer responsibilities.  It is deliberately transport-free so automated tests
can prove state and scheduling behavior without a modem or hardware board.

Inputs
------
``RuntimeSimulation`` receives a complete configuration, currently detected
BLE identities, a latest GNSS fix, a connection state, and elapsed seconds.

Outputs
-------
It exposes the current state/opcode and an ``outbound`` event list.  Events are
``heartbeat`` (command 0x01) and ``location`` (command 0x10); no event is
created by a BLE check unless that check causes entry into outside tracking.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Iterable

from micro_protocol import (
    OPCODE_BEACON,
    OPCODE_GPS_LTE,
    OPCODE_GPS_SAFEZONE,
    OPCODE_TRUSTED,
    ProtocolError,
    decode_application_packet,
)


class DeviceState(str, Enum):
    BEACON = "BEACON"
    TRUSTED_DEVICE = "TRUSTED_DEVICE"
    GPS_SAFE_ZONE = "GPS_SAFE_ZONE"
    GPS_LTE_OUTSIDE = "GPS_LTE_OUTSIDE"


@dataclass(frozen=True)
class SafeZone:
    latitude_e6: int
    longitude_e6: int
    radius_m: int


@dataclass(frozen=True)
class SimulationConfiguration:
    heartbeat_interval_seconds: int
    lte_update_interval_seconds: int
    ble_check_interval_seconds: int
    safe_zones: tuple[SafeZone, ...] = ()
    beacons: tuple[bytes, ...] = ()
    trusted_devices: tuple[bytes, ...] = ()

    def validate(self) -> None:
        if any(not 1 <= value <= 0xFFFF for value in (
            self.heartbeat_interval_seconds,
            self.lte_update_interval_seconds,
            self.ble_check_interval_seconds,
        )):
            raise ValueError("All three intervals must be 1..65535 seconds.")
        if any(len(items) > 4 for items in (self.safe_zones, self.beacons, self.trusted_devices)):
            raise ValueError("A configuration list cannot contain more than four entries.")
        if any(len(identifier) != 6 for identifier in self.beacons + self.trusted_devices):
            raise ValueError("Every Bluetooth identifier must be six bytes.")
        if any(identifier == b"\x00" * 6 for identifier in self.trusted_devices):
            raise ValueError("A configured trusted-device identifier cannot be all zeros.")
        for zone in self.safe_zones:
            if not -90_000_000 <= zone.latitude_e6 <= 90_000_000 or not -180_000_000 <= zone.longitude_e6 <= 180_000_000 or zone.radius_m <= 0:
                raise ValueError("A safe-zone coordinate or radius is invalid.")


@dataclass
class RuntimeEnvironment:
    beacon_detected: bytes | None = None
    trusted_device_detected: bytes | None = None
    latitude_e6: int = 45_421_500
    longitude_e6: int = -75_697_200


@dataclass
class TimerState:
    next_ble_check: int | None = None
    next_heartbeat: int | None = None
    next_lte_location: int | None = None


@dataclass
class OutboundEvent:
    at_seconds: int
    kind: str
    command: int
    state: DeviceState
    opcode: int | None


def opcode_for_state(device_state: DeviceState) -> int:
    return {
        DeviceState.BEACON: OPCODE_BEACON,
        DeviceState.TRUSTED_DEVICE: OPCODE_TRUSTED,
        DeviceState.GPS_SAFE_ZONE: OPCODE_GPS_SAFEZONE,
        DeviceState.GPS_LTE_OUTSIDE: OPCODE_GPS_LTE,
    }[device_state]


class RuntimeSimulation:
    """Reference implementation of configuration mode and runtime mode."""

    def __init__(self, configuration: SimulationConfiguration, environment: RuntimeEnvironment | None = None) -> None:
        configuration.validate()
        self.configuration = configuration
        self.environment = environment or RuntimeEnvironment()
        self.runtime_enabled = False
        self.connected = False
        self.now_seconds = 0
        self.state = DeviceState.GPS_SAFE_ZONE
        self.timers = TimerState()
        self.outbound: list[OutboundEvent] = []

    def set_connected(self, connected: bool) -> None:
        self.connected = connected
        if self.runtime_enabled:
            self.timers.next_heartbeat = self.now_seconds + self.configuration.heartbeat_interval_seconds if connected else None

    def enable_runtime(self) -> None:
        self.runtime_enabled = True
        self.timers.next_ble_check = self.now_seconds + self.configuration.ble_check_interval_seconds
        self.timers.next_heartbeat = (
            self.now_seconds + self.configuration.heartbeat_interval_seconds if self.connected else None
        )
        self._reevaluate_local_state()

    def disable_runtime(self) -> None:
        self.runtime_enabled = False
        self.timers = TimerState()

    def apply_configuration(self, configuration: SimulationConfiguration) -> None:
        """Atomically replace configuration and immediately reevaluate state."""
        configuration.validate()
        self.configuration = configuration
        if not self.runtime_enabled:
            return
        self.timers.next_ble_check = self.now_seconds + configuration.ble_check_interval_seconds
        self.timers.next_heartbeat = self.now_seconds + configuration.heartbeat_interval_seconds if self.connected else None
        self.timers.next_lte_location = (
            self.now_seconds + configuration.lte_update_interval_seconds
            if self.state is DeviceState.GPS_LTE_OUTSIDE else None
        )
        self._reevaluate_local_state()

    def receive_configuration_packet(self, packet: bytes, imei: str) -> None:
        """Apply the complete command-0x02 packet delivered after ``SUP``.

        TCP token/fragment buffering is owned by ``micro_response_parser``;
        this method represents the point after that parser has assembled one
        complete binary packet for the matching device.
        """
        decoded = decode_application_packet(packet, target_imei=imei)
        if not decoded.get("valid") or decoded.get("command") != 0x02:
            raise ProtocolError("The server response did not contain a valid matching command-0x02 packet.")
        update = decoded["configuration_update"]
        self.apply_configuration(configuration_from_values(
            heartbeat=update["heartbeat_interval_seconds"],
            lte=update["lte_update_interval_seconds"],
            ble=update["ble_check_interval_seconds"],
            safe_zones=(
                SafeZone(item["latitude_e6"], item["longitude_e6"], item["radius_m"])
                for item in update["safe_zones"]
            ),
            beacons=(bytes.fromhex(item) for item in update["beacons"]),
            trusted_devices=(bytes.fromhex(item) for item in update["trusted_devices"]),
        ))

    def update_environment(self, **values: object) -> None:
        for name, value in values.items():
            setattr(self.environment, name, value)
        if self.runtime_enabled:
            self._reevaluate_local_state()

    def advance(self, seconds: int) -> None:
        """Advance deterministic time while keeping all timer cadences independent."""
        if seconds < 0:
            raise ValueError("seconds cannot be negative")
        target = self.now_seconds + seconds
        while self.runtime_enabled:
            due = [item for item in (
                self.timers.next_ble_check,
                self.timers.next_heartbeat,
                self.timers.next_lte_location,
            ) if item is not None]
            if not due or min(due) > target:
                break
            self.now_seconds = min(due)
            if self.timers.next_ble_check == self.now_seconds:
                self.timers.next_ble_check += self.configuration.ble_check_interval_seconds
                self._reevaluate_local_state()
            if self.timers.next_heartbeat == self.now_seconds:
                self.timers.next_heartbeat += self.configuration.heartbeat_interval_seconds
                self.outbound.append(OutboundEvent(self.now_seconds, "heartbeat", 0x01, self.state, opcode_for_state(self.state)))
            if self.timers.next_lte_location == self.now_seconds:
                self.timers.next_lte_location += self.configuration.lte_update_interval_seconds
                if self.state is DeviceState.GPS_LTE_OUTSIDE:
                    self.outbound.append(OutboundEvent(self.now_seconds, "location", 0x10, self.state, None))
        self.now_seconds = target

    def _reevaluate_local_state(self) -> None:
        previous = self.state
        if self.environment.beacon_detected in self.configuration.beacons:
            next_state = DeviceState.BEACON
        elif self.environment.trusted_device_detected in self.configuration.trusted_devices:
            next_state = DeviceState.TRUSTED_DEVICE
        elif self._is_inside_any_safe_zone():
            next_state = DeviceState.GPS_SAFE_ZONE
        else:
            next_state = DeviceState.GPS_LTE_OUTSIDE
        self.state = next_state
        if previous is not DeviceState.GPS_LTE_OUTSIDE and next_state is DeviceState.GPS_LTE_OUTSIDE:
            self.timers.next_lte_location = self.now_seconds + self.configuration.lte_update_interval_seconds
            if self.connected:
                self.outbound.append(OutboundEvent(self.now_seconds, "location", 0x10, self.state, None))
        elif previous is DeviceState.GPS_LTE_OUTSIDE and next_state is not DeviceState.GPS_LTE_OUTSIDE:
            self.timers.next_lte_location = None

    def _is_inside_any_safe_zone(self) -> bool:
        for zone in self.configuration.safe_zones:
            north_m = (self.environment.latitude_e6 - zone.latitude_e6) * 111_320 / 1_000_000
            east_m = (self.environment.longitude_e6 - zone.longitude_e6) * 111_320 / 1_000_000
            if (north_m * north_m) + (east_m * east_m) <= zone.radius_m * zone.radius_m:
                return True
        return False


def configuration_from_values(*, heartbeat: int = 60, lte: int = 120, ble: int = 30,
                              safe_zones: Iterable[SafeZone] = (), beacons: Iterable[bytes] = (),
                              trusted_devices: Iterable[bytes] = ()) -> SimulationConfiguration:
    """Concise helper for test and command-line examples."""
    return SimulationConfiguration(heartbeat, lte, ble, tuple(safe_zones), tuple(beacons), tuple(trusted_devices))
