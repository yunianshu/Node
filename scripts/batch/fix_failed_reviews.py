import subprocess, sys, concurrent.futures

chapters = [11, 36, 58, 90, 116, 138, 233, 399, 455, 593, 614, 672, 736, 777, 786, 817, 844, 846, 894, 999, 1078, 1291, 1314, 1421, 1448, 1525, 1669, 1678, 1726, 1952, 1979]

def review_one(ch):
    cmd = [sys.executable, "scripts/pipeline/reviewer.py", "--project", "projects/novels1", "--final", "--chapter", str(ch)]
    result = subprocess.run(cmd, capture_output=True, text=True, timeout=120, encoding="utf-8", errors="replace")
    return ch, result.returncode == 0

with concurrent.futures.ThreadPoolExecutor(max_workers=5) as executor:
    futures = {executor.submit(review_one, ch): ch for ch in chapters}
    for future in concurrent.futures.as_completed(futures):
        ch, ok = future.result()
        status = "OK" if ok else "FAIL"
        print(f"ch{ch}: {status}")

print("Done fixing failed reviews")
