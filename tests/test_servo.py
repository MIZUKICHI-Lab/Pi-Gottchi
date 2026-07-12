import os
import math
import sys
import tempfile
import time
import unittest
from unittest import mock


sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "app"))

from servo import MAX_NS, MIN_NS, PERIOD_NS, Servo, ServoAnimator  # noqa: E402


class ServoTests(unittest.TestCase):
    def make_pwm(self):
        temp = tempfile.TemporaryDirectory()
        chip = temp.name
        with open(os.path.join(chip, "export"), "w", encoding="ascii") as output:
            output.write("")
        channel = os.path.join(chip, "pwm0")
        os.mkdir(channel)
        for name, value in (("enable", "1"), ("period", "0"), ("duty_cycle", "0")):
            with open(os.path.join(channel, name), "w", encoding="ascii") as output:
                output.write(value)
        return temp, chip, channel

    def read_value(self, channel, name):
        with open(os.path.join(channel, name), encoding="ascii") as source:
            return source.read()

    def test_release_then_angle_reenables_pwm(self):
        temp, chip, channel = self.make_pwm()
        self.addCleanup(temp.cleanup)
        servo = Servo(0, pwmchip=chip)
        self.assertEqual("0", self.read_value(channel, "enable"))
        self.assertEqual(str(PERIOD_NS), self.read_value(channel, "period"))

        servo.angle(90)
        expected = int(MIN_NS + (MAX_NS - MIN_NS) / 2)
        self.assertEqual(str(expected), self.read_value(channel, "duty_cycle"))
        self.assertEqual("1", self.read_value(channel, "enable"))

        servo.release()
        self.assertEqual("0", self.read_value(channel, "enable"))
        servo.angle(100)
        self.assertEqual("1", self.read_value(channel, "enable"))
        servo.close()

    def test_invalid_pulse_range_is_rejected(self):
        temp, chip, _channel = self.make_pwm()
        self.addCleanup(temp.cleanup)
        with self.assertRaises(ValueError):
            Servo(0, pwmchip=chip, min_ns=2_000_000, max_ns=1_000_000)

    def test_non_finite_direct_angle_is_rejected(self):
        temp, chip, _channel = self.make_pwm()
        self.addCleanup(temp.cleanup)
        servo = Servo(0, pwmchip=chip)
        with self.assertRaises(ValueError):
            servo.angle(float("nan"))
        servo.close()


class FakeServo:
    def __init__(self):
        self.angles = []
        self.releases = 0
        self.closed = False

    def angle(self, angle):
        self.angles.append(float(angle))

    def release(self):
        self.releases += 1

    def close(self):
        self.closed = True


class ServoAnimatorTests(unittest.TestCase):
    def test_non_finite_env_values_fall_back_to_safe_defaults(self):
        servo = FakeServo()
        servo.channel = 0
        env = {
            "SERVO_ENABLED": "1",
            "SERVO_MIN_ANGLE": "nan",
            "SERVO_CENTER": "nan",
            "SERVO_MAX_PULSE_US": "inf",
        }
        with mock.patch("servo.Servo", return_value=servo) as constructor:
            animator = ServoAnimator.from_env(env)
        self.assertTrue(animator.enabled)
        self.assertTrue(all(math.isfinite(angle) and 70 <= angle <= 110
                            for angle in servo.angles))
        self.assertEqual(2_400_000, constructor.call_args.kwargs["max_ns"])
        self.assertEqual(70, animator.min_angle)
        self.assertEqual(90, animator.center)
        animator.close()

    def test_reaction_stays_in_calibrated_range_and_releases(self):
        servo = FakeServo()
        animator = ServoAnimator(
            servo, center=90, min_angle=80, max_angle=100,
            max_step=20, step_interval=0.01,
        )
        initial_releases = servo.releases
        self.assertTrue(animator.react("happy"))
        deadline = time.monotonic() + 2
        while servo.releases == initial_releases and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertGreater(servo.releases, initial_releases)
        self.assertTrue(all(80 <= angle <= 100 for angle in servo.angles))
        animator.close()
        self.assertTrue(servo.closed)

    def test_disabled_animator_is_a_noop(self):
        animator = ServoAnimator()
        self.assertFalse(animator.react("happy"))
        animator.close()

    def test_from_env_closes_servo_if_initial_release_fails(self):
        class BrokenReleaseServo(FakeServo):
            channel = 0

            def release(self):
                raise OSError("pwm failure")

        servo = BrokenReleaseServo()
        with mock.patch("servo.Servo", return_value=servo):
            animator = ServoAnimator.from_env({"SERVO_ENABLED": "1"})
        self.assertFalse(animator.enabled)
        self.assertTrue(servo.closed)

    def test_worker_failure_releases_and_closes_servo(self):
        class FailAfterStartupServo(FakeServo):
            def angle(self, angle):
                if self.angles:
                    raise OSError("write failed")
                super().angle(angle)

        servo = FailAfterStartupServo()
        animator = ServoAnimator(
            servo, center=90, min_angle=80, max_angle=100,
            max_step=20, step_interval=0.01,
        )
        self.assertTrue(animator.react("happy"))
        deadline = time.monotonic() + 1
        while animator.enabled and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(animator.enabled)
        self.assertTrue(servo.closed)
        animator.close()


if __name__ == "__main__":
    unittest.main()
