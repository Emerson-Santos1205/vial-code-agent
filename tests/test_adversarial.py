import unittest

from benchmark.run_adversarial import run


class AdversarialBenchmarkTests(unittest.TestCase):
    def test_security_violations_are_zero(self) -> None:
        report = run()
        self.assertEqual(report["security_violations"], 0)
        self.assertEqual(report["passed"], report["checks"])
