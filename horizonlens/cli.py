from __future__ import annotations

import argparse
import struct
import sys
import time

from . import rcf6


def _selftest() -> int:
    ok = True

    p = rcf6.Pose(kind=1, sequence=1, fov=60.0)
    body = p.to_body()
    assert len(body) == 64, len(body)
    key = bytes(range(32))
    dg = p.seal(key)
    assert len(dg) == 80, len(dg)

    assert body[0:4] == b"RCF6"
    assert body[4] == 1
    assert body[5] == 1
    seq = struct.unpack_from("<I", body, 12)[0]
    assert seq == 1, seq
    fov = struct.unpack_from("<f", body, 48)[0]
    assert abs(fov - 60.0) < 1e-6, fov
    print("[ok] packet layout: magic/ver/kind/seq/fov at expected offsets")

    assert dg[4] == 2, dg[4]
    import hmac
    sealed_body = body[:4] + bytes((2,)) + body[5:]
    expect = hmac.new(key, sealed_body, "sha256").digest()[:16]
    assert dg[64:80] == expect
    print("[ok] seal: version byte=2, auth tag = HMAC-SHA256(key, body/2)[:16]")

    qx, qy, qz, qw = rcf6.euler_to_quaternion(0, 0, 0)
    assert abs(qx) < 1e-9 and abs(qy) < 1e-9 and abs(qz) < 1e-9 and abs(qw - 1) < 1e-9
    import math
    q = rcf6.euler_to_quaternion(30, 45, 10)
    n = math.sqrt(sum(c * c for c in q))
    assert abs(n - 1.0) < 1e-6, n
    q = rcf6.euler_to_quaternion(0, 90, 0)
    assert abs(q[0]) < 1e-9 and abs(q[2]) < 1e-9
    assert abs(q[1] - math.sin(math.radians(90) / 2)) < 1e-6
    assert abs(q[3] - math.cos(math.radians(90) / 2)) < 1e-6
    print("[ok] euler_to_quaternion matches pose_client convention")

    p2 = rcf6.Pose(kind=rcf6.KIND_DRIVER, sequence=7,
                   quat=rcf6.euler_to_quaternion(5, 10, 0),
                   xyz=(0.1, 0.2, -0.3), fov=75.0)
    p2.seal(key)
    print("[ok] euler pose packs & seals")

    print("\nSELFTEST PASSED" if ok else "\nSELFTEST FAILED")
    return 0 if ok else 1


def _session_roots(args) -> list[str]:
    if args.sessions_root:
        return [args.sessions_root]
    from . import companion
    return [str(companion.session_dir())] + rcf6.default_sessions_roots()


def _discover(args):
    for root in _session_roots(args):
        found = rcf6.discover_session_key(root)
        if found:
            return found
    return None


def _find_key(args) -> int:
    found = _discover(args)
    if not found:
        print("No live camera module session found.")
        print("Start Forza Horizon 6, press Take camera in HorizonLens, then retry.")
        print("Searched:")
        for r in _session_roots(args):
            print(f"  {r}")
        return 1
    key, pid, image_base, path = found
    print("Live companion session:")
    print(f"  game pid     : {pid}")
    print(f"  image base   : 0x{image_base:x}")
    print(f"  session file : {path}")
    print(f"  key (hex)    : {key.hex()}")
    return 0


def _test_pose(args) -> int:
    found = _discover(args)
    if not found:
        print("No live companion session; run find-key for details.")
        return 1
    key, pid, image_base, path = found
    print(f"Driving companion (game pid {pid}) via UDP {rcf6.COMPANION_ADDR[0]}:"
          f"{rcf6.COMPANION_ADDR[1]}")
    quat = rcf6.euler_to_quaternion(args.pitch, args.yaw, args.roll)
    hz = 60.0
    end = time.time() + args.seconds
    sent = 0
    with rcf6.CompanionLink(key) as link:
        while time.time() < end:
            pose = rcf6.Pose(
                kind=args.kind, mode=0, options=0,
                sequence=0,
                recenter=0, xyz=(args.x, args.y, args.z),
                quat=quat, fov=args.fov, move=(0.0, 0.0, 0.0),
            )
            link.send_pose(pose)
            sent += 1
            time.sleep(1.0 / hz)
    print(f"Sent {sent} poses over {args.seconds}s. The hook applies a pose "
          f"only while fresh (<~0.5s), so it releases shortly after this stops.")
    return 0


def main(argv=None) -> int:
    ap = argparse.ArgumentParser(prog="horizonlens")
    sub = ap.add_subparsers(dest="cmd", required=True)

    sub.add_parser("selftest", help="verify the RCF6/2 packet builder")

    fk = sub.add_parser("find-key", help="find the live companion session key")
    fk.add_argument("--sessions-root", default=None)

    tp = sub.add_parser("test-pose", help="send a held test pose to the camera")
    tp.add_argument("--sessions-root", default=None)
    tp.add_argument("--seconds", type=float, default=5.0)
    tp.add_argument("--kind", type=int, default=rcf6.KIND_DRIVER)
    tp.add_argument("--yaw", type=float, default=0.0)
    tp.add_argument("--pitch", type=float, default=0.0)
    tp.add_argument("--roll", type=float, default=0.0)
    tp.add_argument("--x", type=float, default=0.0)
    tp.add_argument("--y", type=float, default=0.0)
    tp.add_argument("--z", type=float, default=0.0)
    tp.add_argument("--fov", type=float, default=60.0)

    args = ap.parse_args(argv)
    if args.cmd == "selftest":
        return _selftest()
    if args.cmd == "find-key":
        return _find_key(args)
    if args.cmd == "test-pose":
        return _test_pose(args)
    return 2


if __name__ == "__main__":
    sys.exit(main())
