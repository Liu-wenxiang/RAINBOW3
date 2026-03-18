import argparse
import hid

# 默认目标设备（可按需修改）
DEFAULT_VID = 0x413D
DEFAULT_PID = 0x2104

PREFERRED_USAGE_PAGE_ORDER = [0xFF7A, 0xFFB1]


def choose_candidate_paths(vid: int, pid: int):
    candidates = [d for d in hid.enumerate() if d.get("vendor_id") == vid and d.get("product_id") == pid]
    if not candidates:
        return []

    def score(d: dict) -> int:
        usage_page = int(d.get("usage_page", 0) or 0)
        path = d.get("path")
        path_s = path.decode("utf-8", errors="ignore") if isinstance(path, (bytes, bytearray)) else str(path)

        s = 0
        if usage_page in PREFERRED_USAGE_PAGE_ORDER:
            s += 200 - PREFERRED_USAGE_PAGE_ORDER.index(usage_page) * 50
        if usage_page >= 0xFF00:
            s += 50
        if "\\KBD" in path_s:
            s -= 100
        return s

    sorted_candidates = sorted(candidates, key=score, reverse=True)
    return [d.get("path") for d in sorted_candidates if d.get("path") is not None]

def parse_hex(hex_str: str) -> bytes:
    s = hex_str.strip().replace(":", " ").replace(",", " ")
    s = "".join(s.split())
    return bytes.fromhex(s)


def build_output(hex_str: str, payload_len: int = 64, report_id: int = 0) -> bytes:
    payload = parse_hex(hex_str)
    if len(payload) < payload_len:
        payload = payload + b"\x00" * (payload_len - len(payload))
    elif len(payload) > payload_len:
        payload = payload[:payload_len]
    return bytes([report_id & 0xFF]) + payload


def _write_sequence_with_device(dev: hid.device, outputs: list[bytes]) -> list[int]:
    results: list[int] = []
    for out in outputs:
        written = dev.write(out)
        if written < 0:
            raise OSError("HID write failed (returned -1)")
        results.append(written)
    return results


def send_outputs(outputs: list[bytes], vid: int = DEFAULT_VID, pid: int = DEFAULT_PID, path: str | bytes | None = None) -> list[int]:
    if not outputs:
        return []

    if path:
        dev = hid.device()
        try:
            dev.open_path(path.encode() if isinstance(path, str) else path)
            return _write_sequence_with_device(dev, outputs)
        finally:
            try:
                dev.close()
            except Exception:
                pass

    candidate_paths = choose_candidate_paths(vid, pid)
    if candidate_paths:
        for candidate_path in candidate_paths:
            dev = hid.device()
            try:
                dev.open_path(candidate_path)
                return _write_sequence_with_device(dev, outputs)
            except Exception:
                try:
                    dev.close()
                except Exception:
                    pass
                continue
            finally:
                try:
                    dev.close()
                except Exception:
                    pass

        raise OSError(
            "HID write failed (returned -1).\n"
            "- Auto-selected interfaces did not accept this report. Try the exact --path from --list.\n"
            "- Note: this script sends 1(report_id)+payload_len bytes in total. Some devices expect a different size."
        )

    dev = hid.device()
    try:
        dev.open(vid, pid)
        return _write_sequence_with_device(dev, outputs)
    finally:
        try:
            dev.close()
        except Exception:
            pass


def send_hex_payloads(hex_payloads: list[str], vid: int = DEFAULT_VID, pid: int = DEFAULT_PID, path: str | bytes | None = None, report_id: int = 0, payload_len: int = 64) -> list[int]:
    outputs = [build_output(payload, payload_len=payload_len, report_id=report_id) for payload in hex_payloads]
    return send_outputs(outputs, vid=vid, pid=pid, path=path)

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--list", action="store_true", help="List HID devices")
    ap.add_argument(
        "--vid",
        type=lambda x: int(x, 16),
        default=DEFAULT_VID,
        help=f"Vendor ID (default 0x{DEFAULT_VID:04x}), e.g. 0x1234",
    )
    ap.add_argument(
        "--pid",
        type=lambda x: int(x, 16),
        default=DEFAULT_PID,
        help=f"Product ID (default 0x{DEFAULT_PID:04x}), e.g. 0xabcd",
    )
    ap.add_argument("--path", type=str, help="Open by device path (more precise than VID/PID)")
    ap.add_argument("--report-id", type=lambda x: int(x, 0), default=0, help="Report ID byte, default 0")
    ap.add_argument("--hex", type=str, help="Payload hex string (HID Data), e.g. 'a50d...cff faa...'")
    ap.add_argument("--len", type=int, default=64, help="Payload length to send (default 64)")
    args = ap.parse_args()

    if args.list:
        for i, d in enumerate(hid.enumerate()):
            path = d.get('path')
            if isinstance(path, (bytes, bytearray)):
                path_str = path.decode('utf-8', errors='backslashreplace')
            else:
                path_str = str(path)
            print(f"[{i}] vid=0x{d['vendor_id']:04x} pid=0x{d['product_id']:04x} "
                  f"usage_page=0x{d.get('usage_page',0):x} usage=0x{d.get('usage',0):x} "
                  f"product={d.get('product_string','')} path={path_str}")
        return

    if not args.hex:
        raise SystemExit("Missing --hex. Paste Wireshark 'HID Data' hex here.")

    try:
        results = send_hex_payloads([args.hex], vid=args.vid, pid=args.pid, path=args.path, report_id=args.report_id, payload_len=args.len)
    except OSError as e:
        raise SystemExit(str(e)) from e

    n = results[0] if results else 0
    print("write bytes:", n)

if __name__ == "__main__":
    main()