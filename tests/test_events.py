from __future__ import annotations

import unittest

from vial_code_agent.events import EventStore


class EventStoreTests(unittest.TestCase):
    def setUp(self) -> None:
        self.store = EventStore()
        self.store.configure({"vial-code-agent", "org-root"})

    def test_publish_and_delta(self) -> None:
        self.store.publish(
            "RESOURCE_UPDATED", "RES-042", 17, "vial-code-agent",
            data={"hint": "API criada"})
        self.store.publish(
            "RESOURCE_UPDATED", "RES-042", 18, "vial-code-agent")
        self.assertEqual(self.store.stats()["events"], 2)
        events = self.store.delta()
        self.assertEqual(events[0].resource, "RES-042")
        self.assertEqual(events[0].data["hint"], "API criada")
        after = self.store.delta(after_event_id=events[0].event_id)
        self.assertEqual([event.version for event in after], [18])

    def test_publish_is_idempotent(self) -> None:
        first = self.store.publish("E", "RES", 1, "vial-code-agent")
        second = self.store.publish("E", "RES", 1, "vial-code-agent")
        self.assertEqual(first.event_id, second.event_id)
        self.assertEqual(self.store.stats()["events"], 1)

    def test_rejects_unauthorized_actor(self) -> None:
        with self.assertRaises(PermissionError):
            self.store.publish("E", "RES", 1, "intruder")

    def test_latest_and_version(self) -> None:
        self.store.publish("E", "RES", 1, "vial-code-agent")
        self.store.publish("E", "RES", 2, "vial-code-agent")
        self.assertEqual(self.store.version_of("RES"), 2)
        self.assertEqual(self.store.latest("RES").version, 2)
        self.assertIsNone(self.store.latest("OTHER"))

    def test_round_trip_via_list(self) -> None:
        self.store.publish("E", "RES", 1, "vial-code-agent", data={"k": "v"})
        restored = EventStore.from_list(self.store.to_list())
        restored.configure({"vial-code-agent"})
        self.assertEqual(restored.stats()["events"], 1)
        self.assertEqual(restored.delta()[0].data["k"], "v")


if __name__ == "__main__":
    unittest.main()