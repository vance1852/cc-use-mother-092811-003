import unittest

from transport_coordination.charging_acceptance import run


class ChargingAcceptanceTest(unittest.TestCase):
    def test_offline_acceptance(self):
        result = run()
        self.assertEqual("ok", result["status"])
        self.assertTrue(result["audit_valid"])
        self.assertTrue(result["confirm_replayed"])
        self.assertTrue(result["idempotent_occupancy"])
        self.assertTrue(result["normal_displaced"])
        self.assertTrue(result["rescue_started_session_protected"])
        self.assertTrue(result["fault_replan_feasible"])
        self.assertTrue(result["safety_safely_reachable"])
        self.assertEqual(1, result["active_reservations_after_restart"])
        self.assertGreater(result["occupancy_after_restart"], 0)


if __name__ == "__main__":
    unittest.main()
