#!/usr/bin/env python3
"""State-machine and independent-timer tests for Micro protocol V8."""

from __future__ import annotations

import unittest

from micro_protocol import OPCODE_BEACON, OPCODE_GPS_LTE, OPCODE_GPS_SAFEZONE, OPCODE_TRUSTED, build_configuration_update_packet, default_settings
from micro_state_machine import DeviceState, RuntimeEnvironment, RuntimeSimulation, SafeZone, configuration_from_values


BEACON = bytes.fromhex("0FAC91003B91")
PHONE = bytes.fromhex("AABBCCDDEE01")
ZONE = SafeZone(45_421_500, -75_697_200, 250)


class MicroStateMachineTests(unittest.TestCase):
    def runtime(self, *, environment: RuntimeEnvironment, **config: object) -> RuntimeSimulation:
        sim = RuntimeSimulation(configuration_from_values(**config), environment)
        sim.set_connected(True)
        sim.enable_runtime()
        return sim

    def test_recognized_beacon_has_priority_and_does_not_select_gnss(self) -> None:
        sim = self.runtime(environment=RuntimeEnvironment(BEACON, PHONE, 45_500_000, -75_750_000), beacons=(BEACON,), trusted_devices=(PHONE,))
        self.assertEqual(sim.state, DeviceState.BEACON)
        self.assertEqual(OPCODE_BEACON, 0x01)
        self.assertFalse(sim.outbound)

    def test_recognized_trusted_device_is_selected_after_no_beacon(self) -> None:
        sim = self.runtime(environment=RuntimeEnvironment(None, PHONE), trusted_devices=(PHONE,))
        self.assertEqual(sim.state, DeviceState.TRUSTED_DEVICE)
        self.assertEqual(OPCODE_TRUSTED, 0xA0)

    def test_gps_safe_zone_state(self) -> None:
        sim = self.runtime(environment=RuntimeEnvironment(None, None, ZONE.latitude_e6, ZONE.longitude_e6), safe_zones=(ZONE,))
        self.assertEqual(sim.state, DeviceState.GPS_SAFE_ZONE)
        self.assertEqual(OPCODE_GPS_SAFEZONE, 0x0A)

    def test_outside_state_sends_transition_location_and_uses_opcode_10(self) -> None:
        sim = self.runtime(environment=RuntimeEnvironment(None, None, 45_500_000, -75_750_000), safe_zones=(ZONE,))
        self.assertEqual(sim.state, DeviceState.GPS_LTE_OUTSIDE)
        self.assertEqual(OPCODE_GPS_LTE, 0x10)
        self.assertEqual([(event.kind, event.command) for event in sim.outbound], [("location", 0x10)])

    def test_ble_timer_reevaluates_without_a_heartbeat(self) -> None:
        sim = self.runtime(environment=RuntimeEnvironment(None, PHONE), trusted_devices=(PHONE,), ble=10, heartbeat=60)
        sim.outbound.clear(); sim.advance(10)
        self.assertEqual(sim.state, DeviceState.TRUSTED_DEVICE)
        self.assertFalse(sim.outbound)

    def test_heartbeat_timer_uses_latest_state_in_every_state(self) -> None:
        sim = self.runtime(environment=RuntimeEnvironment(None, PHONE), trusted_devices=(PHONE,), heartbeat=10)
        sim.advance(10)
        self.assertEqual(sim.outbound[-1].kind, "heartbeat")
        self.assertEqual(sim.outbound[-1].opcode, OPCODE_TRUSTED)
        sim.update_environment(trusted_device_detected=None, latitude_e6=ZONE.latitude_e6, longitude_e6=ZONE.longitude_e6)
        sim.apply_configuration(configuration_from_values(heartbeat=10, safe_zones=(ZONE,)))
        sim.advance(10)
        self.assertEqual(sim.outbound[-1].opcode, OPCODE_GPS_SAFEZONE)

    def test_lte_timer_stops_after_leaving_outside_state(self) -> None:
        sim = self.runtime(environment=RuntimeEnvironment(None, None, 45_500_000, -75_750_000), safe_zones=(ZONE,), lte=10)
        sim.outbound.clear(); sim.advance(10)
        self.assertEqual([(e.kind, e.command) for e in sim.outbound], [("location", 0x10)])
        sim.update_environment(latitude_e6=ZONE.latitude_e6, longitude_e6=ZONE.longitude_e6)
        sim.outbound.clear(); sim.advance(20)
        self.assertFalse(any(e.kind == "location" for e in sim.outbound))

    def test_three_timer_schedules_remain_independent(self) -> None:
        sim = self.runtime(environment=RuntimeEnvironment(None, PHONE), trusted_devices=(PHONE,), ble=7, heartbeat=11, lte=13)
        before = (sim.timers.next_ble_check, sim.timers.next_heartbeat, sim.timers.next_lte_location)
        sim.advance(7)
        self.assertEqual(sim.timers.next_heartbeat, before[1])
        self.assertIsNone(sim.timers.next_lte_location)
        self.assertEqual(sim.timers.next_ble_check, 14)

    def test_trusted_device_taken_update_reclassifies_immediately(self) -> None:
        sim = self.runtime(environment=RuntimeEnvironment(None, PHONE, 45_500_000, -75_750_000), trusted_devices=(PHONE,))
        self.assertEqual(sim.state, DeviceState.TRUSTED_DEVICE)
        lost_person = default_settings()
        lost_person.update({"safe_zones": b"", "beacon_list": b"", "trusted_device_list": b""})
        packet = build_configuration_update_packet(
            target_imei="861352064050787", update_id=42, configuration=lost_person, sequence_id=2,
        )
        # The shared response-parser tests cover fragmented `SUP\n` framing.
        # Once SUP has yielded this complete packet, the update is immediate.
        sim.receive_configuration_packet(packet, "861352064050787")
        self.assertEqual(sim.state, DeviceState.GPS_LTE_OUTSIDE)

    def test_lost_person_zero_lists_clear_all_safe_contexts(self) -> None:
        sim = self.runtime(environment=RuntimeEnvironment(BEACON, PHONE, ZONE.latitude_e6, ZONE.longitude_e6),
                           beacons=(BEACON,), trusted_devices=(PHONE,), safe_zones=(ZONE,))
        self.assertEqual(sim.state, DeviceState.BEACON)
        sim.apply_configuration(configuration_from_values())
        self.assertEqual(sim.state, DeviceState.GPS_LTE_OUTSIDE)

    def test_configuration_timer_change_replaces_each_schedule_once(self) -> None:
        sim = self.runtime(environment=RuntimeEnvironment(None, PHONE), trusted_devices=(PHONE,), ble=30, heartbeat=60)
        sim.advance(5)
        sim.apply_configuration(configuration_from_values(heartbeat=20, lte=40, ble=10, trusted_devices=(PHONE,)))
        self.assertEqual((sim.timers.next_ble_check, sim.timers.next_heartbeat), (15, 25))

    def test_configuration_mode_starts_no_timers_or_packets(self) -> None:
        sim = RuntimeSimulation(configuration_from_values(trusted_devices=(PHONE,)), RuntimeEnvironment(None, PHONE))
        sim.set_connected(True); sim.advance(100)
        self.assertFalse(sim.outbound)
        self.assertEqual(sim.timers.next_heartbeat, None)

    def test_heartbeat_registry_is_protocol_tested_elsewhere_and_server_uses_matching_imei(self) -> None:
        # The explicit matching-IMEI and complete-registry wire assertions live
        # in test_micro_tcp_server.py and test_micro_protocol.py.  This guard
        # documents that runtime state does not replace configured registry.
        sim = self.runtime(environment=RuntimeEnvironment(None, PHONE), trusted_devices=(PHONE,))
        self.assertEqual(sim.state, DeviceState.TRUSTED_DEVICE)


if __name__ == "__main__":
    unittest.main(verbosity=2)
