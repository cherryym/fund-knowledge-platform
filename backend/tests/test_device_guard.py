"""Process-wide MPS serialization: concurrent local-model GPU sections never overlap."""
import threading
import time

from fund_kb.device_guard import gpu_section


def _overlaps(device_a, device_b):
    active, peak, lock = [0], [0], threading.Lock()
    start = threading.Barrier(2)
    def work(device):
        start.wait()
        with gpu_section(device):
            with lock:
                active[0] += 1
                peak[0] = max(peak[0], active[0])
            time.sleep(0.05)
            with lock:
                active[0] -= 1
    threads = [threading.Thread(target=work, args=(device,)) for device in (device_a, device_b)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join()
    return peak[0]


def test_mps_sections_are_serialized_across_models_and_threads():
    assert _overlaps("mps", "mps") == 1


def test_cpu_sections_are_not_serialized_and_reentry_is_allowed():
    assert _overlaps("cpu", "cpu") == 2
    with gpu_section("mps"), gpu_section("mps"):  # reentrant: nested model calls cannot self-deadlock
        pass
