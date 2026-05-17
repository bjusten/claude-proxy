"""Tests for journal/pollers.py — NvidiaSmiPoller and SystemInfoPoller."""

import threading
import time
import unittest

import subprocess
import unittest.mock as mock

from journal.pollers import (
    NvidiaSmiPoller,
    SystemInfoPoller,
    default_nvidia_smi_sampler,
    make_default_system_sampler,
    parse_nvidia_smi_csv,
    parse_proc_loadavg,
    parse_proc_meminfo,
    parse_proc_uptime,
)


def _sys(uptime=12345.0, util=42):
    """A complete system sample with knobs to change individual fields."""
    return {
        "uptime_seconds": uptime,
        "loadavg": [0.42, 0.55, 0.61],
        "memory": {
            "total_bytes": 16777216000,
            "available_bytes": 12582912000,
            "used_bytes": 4194304000,
            "free_bytes": 8388608000,
        },
        "disks": [
            {"mount": "/", "total_bytes": 100, "used_bytes": util,
             "free_bytes": 100 - util},
        ],
    }


def _gpu(util):
    return [{
        "index": 0, "name": "RTX A6000",
        "utilization_gpu": util,
        "memory_used_mib": 1000, "memory_total_mib": 49000,
        "temperature_c": 60, "power_draw_w": 100.0, "power_limit_w": 300.0,
    }]


class TestNvidiaSmiPollerChangeDetection(unittest.TestCase):
    def test_identical_samples_emit_once_changed_emit_per_tick(self):
        # Sequence: 10, 10, 20, 20, 30 → exactly 3 emissions
        seq = [_gpu(10), _gpu(10), _gpu(20), _gpu(20), _gpu(30)]
        idx = {"n": 0}
        emitted = []
        third = threading.Event()

        def sampler():
            n = idx["n"]
            idx["n"] += 1
            return seq[min(n, len(seq) - 1)]

        def on_sample(sample):
            emitted.append(sample)
            if len(emitted) >= 3:
                third.set()

        stop = threading.Event()
        poller = NvidiaSmiPoller(
            sampler=sampler, on_sample=on_sample,
            interval=0.01, stop_event=stop,
        )
        t = poller.start()
        self.assertTrue(third.wait(timeout=2.0),
                        f"only got {len(emitted)} emissions: {emitted}")
        stop.set()
        t.join(timeout=2.0)

        # Exactly three emits (10, 20, 30); the repeats were suppressed.
        utils = [s["gpus"][0]["utilization_gpu"] for s in emitted[:3]]
        self.assertEqual(utils, [10, 20, 30])
        # Each emission carries a `ts` and a `gpus` list (no id/type yet —
        # those are added by the proxy wiring).
        for s in emitted[:3]:
            self.assertIn("ts", s)
            self.assertIn("gpus", s)
            self.assertNotIn("id", s)
            self.assertNotIn("type", s)


class TestNvidiaSmiPollerExceptionRecovery(unittest.TestCase):
    def test_sampler_exception_is_caught_and_loop_continues(self):
        # Tick 1: raises. Tick 2: returns sample. Loop must keep ticking.
        idx = {"n": 0}
        emitted = []
        got = threading.Event()

        def sampler():
            n = idx["n"]
            idx["n"] += 1
            if n == 0:
                raise RuntimeError("transient sampler failure")
            return _gpu(42)

        def on_sample(sample):
            emitted.append(sample)
            got.set()

        stop = threading.Event()
        poller = NvidiaSmiPoller(
            sampler=sampler, on_sample=on_sample,
            interval=0.01, stop_event=stop,
        )
        t = poller.start()
        self.assertTrue(got.wait(timeout=2.0),
                        "loop did not recover after sampler exception")
        # Thread must still be alive at the moment of recovery.
        self.assertTrue(t.is_alive())
        stop.set()
        t.join(timeout=2.0)

        self.assertGreaterEqual(len(emitted), 1)
        self.assertEqual(emitted[0]["gpus"][0]["utilization_gpu"], 42)


class TestNvidiaSmiPollerMissingBinary(unittest.TestCase):
    def test_filenotfound_triggers_single_warning_and_thread_exits(self):
        calls = {"n": 0}

        def sampler():
            calls["n"] += 1
            raise FileNotFoundError("nvidia-smi")

        emitted = []

        stop = threading.Event()
        poller = NvidiaSmiPoller(
            sampler=sampler, on_sample=emitted.append,
            interval=0.01, stop_event=stop,
        )
        with self.assertLogs("journal.pollers", level="WARNING") as caplog:
            t = poller.start()
            t.join(timeout=2.0)

        self.assertFalse(t.is_alive(), "poller thread should exit when nvidia-smi is missing")
        self.assertEqual(calls["n"], 1, "sampler must not be retried after missing-binary")
        self.assertEqual(emitted, [])
        # Exactly one WARNING-level record from this module.
        self.assertEqual(len(caplog.records), 1)
        self.assertIn("nvidia-smi", caplog.records[0].getMessage().lower())


class TestNvidiaSmiPollerNonZeroExit(unittest.TestCase):
    def test_calledprocesserror_terminates_poller_with_warning(self):
        calls = {"n": 0}

        def sampler():
            calls["n"] += 1
            raise subprocess.CalledProcessError(returncode=9, cmd=["nvidia-smi"])

        emitted = []
        stop = threading.Event()
        poller = NvidiaSmiPoller(
            sampler=sampler, on_sample=emitted.append,
            interval=0.01, stop_event=stop,
        )
        with self.assertLogs("journal.pollers", level="WARNING") as caplog:
            t = poller.start()
            t.join(timeout=2.0)

        self.assertFalse(t.is_alive())
        self.assertEqual(calls["n"], 1)
        self.assertEqual(emitted, [])
        self.assertEqual(len(caplog.records), 1)


class TestNvidiaSmiPollerLatest(unittest.TestCase):
    def test_latest_is_empty_before_first_sample(self):
        poller = NvidiaSmiPoller(
            sampler=lambda: _gpu(0), on_sample=lambda _s: None,
            interval=30.0, stop_event=threading.Event(),
        )
        self.assertEqual(poller.latest, {})

    def test_latest_reflects_most_recent_emission(self):
        seq = [_gpu(10), _gpu(20)]
        idx = {"n": 0}
        ready = threading.Event()

        def sampler():
            n = idx["n"]
            idx["n"] += 1
            return seq[min(n, len(seq) - 1)]

        emit_count = {"n": 0}

        def on_sample(_s):
            emit_count["n"] += 1
            if emit_count["n"] >= 2:
                ready.set()

        stop = threading.Event()
        poller = NvidiaSmiPoller(
            sampler=sampler, on_sample=on_sample,
            interval=0.01, stop_event=stop,
        )
        t = poller.start()
        self.assertTrue(ready.wait(timeout=2.0))
        stop.set()
        t.join(timeout=2.0)
        self.assertEqual(poller.latest["gpus"][0]["utilization_gpu"], 20)
        self.assertIn("ts", poller.latest)


class TestNvidiaSmiPollerShutdown(unittest.TestCase):
    def test_stop_event_unblocks_long_interval_wait(self):
        # interval is much longer than the test deadline; only the stop_event
        # can wake the loop. This guards against SIGTERM blocking the proxy.
        def sampler():
            return _gpu(0)

        stop = threading.Event()
        poller = NvidiaSmiPoller(
            sampler=sampler, on_sample=lambda _s: None,
            interval=30.0, stop_event=stop,
        )
        t = poller.start()
        time.sleep(0.05)  # let the loop reach the wait()
        stop.set()
        t.join(timeout=1.0)
        self.assertFalse(t.is_alive(), "poller did not respond to stop_event")


class TestParseNvidiaSmiCsv(unittest.TestCase):
    def test_parse_two_gpu_rows(self):
        # `--format=csv,noheader,nounits` output: comma-space separated, no header.
        csv = (
            "0, NVIDIA RTX A6000, 47, 12000, 24000, 62, 180.50, 300.00\n"
            "1, NVIDIA RTX A6000, 3, 200, 24000, 41, 35.20, 300.00\n"
        )
        gpus = parse_nvidia_smi_csv(csv)
        self.assertEqual(gpus, [
            {"index": 0, "name": "NVIDIA RTX A6000",
             "utilization_gpu": 47, "memory_used_mib": 12000,
             "memory_total_mib": 24000, "temperature_c": 62,
             "power_draw_w": 180.5, "power_limit_w": 300.0},
            {"index": 1, "name": "NVIDIA RTX A6000",
             "utilization_gpu": 3, "memory_used_mib": 200,
             "memory_total_mib": 24000, "temperature_c": 41,
             "power_draw_w": 35.2, "power_limit_w": 300.0},
        ])

    def test_blank_lines_ignored(self):
        csv = "0, T4, 5, 100, 16000, 38, 25.0, 70.0\n\n  \n"
        gpus = parse_nvidia_smi_csv(csv)
        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0]["name"], "T4")


class TestDefaultNvidiaSmiSampler(unittest.TestCase):
    EXPECTED_ARGV = [
        "nvidia-smi",
        "--query-gpu=index,name,utilization.gpu,memory.used,memory.total,temperature.gpu,power.draw,power.limit",
        "--format=csv,noheader,nounits",
    ]

    def _fake_completed(self, stdout="0, T4, 5, 100, 16000, 38, 25.0, 70.0\n", returncode=0):
        m = mock.MagicMock()
        m.returncode = returncode
        m.stdout = stdout
        return m

    def test_default_sampler_runs_nvidia_smi_with_expected_argv(self):
        captured = {}

        def fake_run(argv, **kwargs):
            captured["argv"] = list(argv)
            captured["kwargs"] = kwargs
            return self._fake_completed()

        with mock.patch("subprocess.run", side_effect=fake_run):
            gpus = default_nvidia_smi_sampler()

        self.assertEqual(captured["argv"], self.EXPECTED_ARGV)
        # text=True (or universal_newlines=True) so we get str, not bytes.
        self.assertTrue(
            captured["kwargs"].get("text") is True
            or captured["kwargs"].get("universal_newlines") is True
        )
        self.assertEqual(len(gpus), 1)
        self.assertEqual(gpus[0]["name"], "T4")


class TestParseProcUptime(unittest.TestCase):
    def test_first_field_is_uptime_seconds(self):
        # /proc/uptime is "<uptime_seconds> <idle_seconds>".
        self.assertEqual(parse_proc_uptime("12345.67 23456.78\n"), 12345.67)

    def test_trailing_whitespace_ignored(self):
        self.assertEqual(parse_proc_uptime("0.42 0.55   \n"), 0.42)


class TestParseProcLoadavg(unittest.TestCase):
    def test_returns_first_three_floats(self):
        # /proc/loadavg: "1min 5min 15min running/total last_pid"
        self.assertEqual(
            parse_proc_loadavg("0.42 0.55 0.61 1/234 5678\n"),
            [0.42, 0.55, 0.61],
        )


class TestParseProcMeminfo(unittest.TestCase):
    def test_extracts_total_available_free_and_computes_used(self):
        # Values are in kB in /proc/meminfo. Parser converts to bytes.
        text = (
            "MemTotal:       16384000 kB\n"
            "MemFree:         8000000 kB\n"
            "MemAvailable:   12000000 kB\n"
            "Buffers:          100000 kB\n"
        )
        mem = parse_proc_meminfo(text)
        self.assertEqual(mem["total_bytes"], 16384000 * 1024)
        self.assertEqual(mem["free_bytes"], 8000000 * 1024)
        self.assertEqual(mem["available_bytes"], 12000000 * 1024)
        # used = total - available (the usable-pressure definition; matches the
        # `free -h` "used" column).
        self.assertEqual(mem["used_bytes"], (16384000 - 12000000) * 1024)


class TestSystemInfoPollerChangeDetection(unittest.TestCase):
    def test_identical_samples_emit_once_changed_emit_per_tick(self):
        seq = [_sys(util=10), _sys(util=10), _sys(util=20), _sys(util=20), _sys(util=30)]
        idx = {"n": 0}
        emitted = []
        third = threading.Event()

        def sampler():
            n = idx["n"]
            idx["n"] += 1
            return seq[min(n, len(seq) - 1)]

        def on_sample(sample):
            emitted.append(sample)
            if len(emitted) >= 3:
                third.set()

        stop = threading.Event()
        poller = SystemInfoPoller(
            sampler=sampler, on_sample=on_sample,
            interval=0.01, stop_event=stop,
        )
        t = poller.start()
        self.assertTrue(third.wait(timeout=2.0),
                        f"only got {len(emitted)} emissions: {emitted}")
        stop.set()
        t.join(timeout=2.0)

        utils = [s["disks"][0]["used_bytes"] for s in emitted[:3]]
        self.assertEqual(utils, [10, 20, 30])
        # Sample shape: top-level keys merged with `ts`. Wrapping into the
        # journal `id`/`type` envelope is the proxy's job.
        for s in emitted[:3]:
            self.assertIn("ts", s)
            self.assertIn("uptime_seconds", s)
            self.assertIn("loadavg", s)
            self.assertIn("memory", s)
            self.assertIn("disks", s)
            self.assertNotIn("id", s)
            self.assertNotIn("type", s)


class TestSystemInfoPollerExceptionRecovery(unittest.TestCase):
    def test_sampler_exception_is_caught_and_loop_continues(self):
        idx = {"n": 0}
        emitted = []
        got = threading.Event()

        def sampler():
            n = idx["n"]
            idx["n"] += 1
            if n == 0:
                raise RuntimeError("transient /proc read failure")
            return _sys(util=77)

        def on_sample(sample):
            emitted.append(sample)
            got.set()

        stop = threading.Event()
        poller = SystemInfoPoller(
            sampler=sampler, on_sample=on_sample,
            interval=0.01, stop_event=stop,
        )
        t = poller.start()
        self.assertTrue(got.wait(timeout=2.0),
                        "loop did not recover after sampler exception")
        self.assertTrue(t.is_alive())
        stop.set()
        t.join(timeout=2.0)

        self.assertGreaterEqual(len(emitted), 1)
        self.assertEqual(emitted[0]["disks"][0]["used_bytes"], 77)


class TestSystemInfoPollerLatest(unittest.TestCase):
    def test_latest_is_empty_before_first_sample(self):
        poller = SystemInfoPoller(
            sampler=lambda: _sys(util=0), on_sample=lambda _s: None,
            interval=30.0, stop_event=threading.Event(),
        )
        self.assertEqual(poller.latest, {})

    def test_latest_reflects_most_recent_emission(self):
        seq = [_sys(util=10), _sys(util=20)]
        idx = {"n": 0}
        ready = threading.Event()

        def sampler():
            n = idx["n"]
            idx["n"] += 1
            return seq[min(n, len(seq) - 1)]

        emit_count = {"n": 0}

        def on_sample(_s):
            emit_count["n"] += 1
            if emit_count["n"] >= 2:
                ready.set()

        stop = threading.Event()
        poller = SystemInfoPoller(
            sampler=sampler, on_sample=on_sample,
            interval=0.01, stop_event=stop,
        )
        t = poller.start()
        self.assertTrue(ready.wait(timeout=2.0))
        stop.set()
        t.join(timeout=2.0)
        self.assertEqual(poller.latest["disks"][0]["used_bytes"], 20)
        self.assertIn("ts", poller.latest)


class TestSystemInfoPollerShutdown(unittest.TestCase):
    def test_stop_event_unblocks_long_interval_wait(self):
        # Long interval; only stop_event can wake the loop. Guards against
        # SIGTERM blocking on the next sample.
        stop = threading.Event()
        poller = SystemInfoPoller(
            sampler=lambda: _sys(), on_sample=lambda _s: None,
            interval=30.0, stop_event=stop,
        )
        t = poller.start()
        time.sleep(0.05)
        stop.set()
        t.join(timeout=1.0)
        self.assertFalse(t.is_alive(), "poller did not respond to stop_event")


class TestMakeDefaultSystemSamplerMountDegradation(unittest.TestCase):
    """The default sampler degrades gracefully when a mount disappears.

    First failure on a given mount emits exactly one WARNING; subsequent
    samples omit that mount entirely. Healthy mounts keep reporting.
    """

    def _fake_proc_reader(self):
        def reader(path):
            if path == "/proc/uptime":
                return "100.0 50.0\n"
            if path == "/proc/loadavg":
                return "0.1 0.2 0.3 1/2 3\n"
            if path == "/proc/meminfo":
                return ("MemTotal:        1024 kB\n"
                        "MemFree:          512 kB\n"
                        "MemAvailable:     768 kB\n")
            raise FileNotFoundError(path)
        return reader

    def test_missing_mount_logged_once_and_omitted(self):
        calls = []

        def disk_usage(mount):
            calls.append(mount)
            if mount == "/bogus":
                raise FileNotFoundError("/bogus")
            # shutil.disk_usage returns a 3-tuple-like namedtuple.
            return mock.MagicMock(total=100, used=40, free=60)

        sampler = make_default_system_sampler(
            disk_mounts=["/", "/bogus"],
            disk_usage=disk_usage,
            read_text=self._fake_proc_reader(),
        )

        with self.assertLogs("journal.pollers", level="WARNING") as caplog:
            s1 = sampler()
            s2 = sampler()
            s3 = sampler()

        # Healthy mount present in every sample.
        for s in (s1, s2, s3):
            self.assertEqual([d["mount"] for d in s["disks"]], ["/"])
            self.assertEqual(s["disks"][0]["total_bytes"], 100)
            self.assertEqual(s["disks"][0]["used_bytes"], 40)
            self.assertEqual(s["disks"][0]["free_bytes"], 60)

        # disk_usage("/bogus") is attempted exactly once across all three
        # samples — after the first failure it is skipped.
        self.assertEqual(calls.count("/bogus"), 1)
        # Exactly ONE warning for the missing mount.
        bogus_warnings = [r for r in caplog.records if "/bogus" in r.getMessage()]
        self.assertEqual(len(bogus_warnings), 1)

    def test_proc_fields_populated_from_reader(self):
        sampler = make_default_system_sampler(
            disk_mounts=[],
            disk_usage=lambda _m: mock.MagicMock(total=0, used=0, free=0),
            read_text=self._fake_proc_reader(),
        )
        s = sampler()
        self.assertEqual(s["uptime_seconds"], 100.0)
        self.assertEqual(s["loadavg"], [0.1, 0.2, 0.3])
        self.assertEqual(s["memory"]["total_bytes"], 1024 * 1024)
        self.assertEqual(s["memory"]["free_bytes"], 512 * 1024)
        self.assertEqual(s["memory"]["available_bytes"], 768 * 1024)
        self.assertEqual(s["memory"]["used_bytes"], (1024 - 768) * 1024)
        self.assertEqual(s["disks"], [])


if __name__ == "__main__":
    unittest.main()
