import argparse
import socket
import time


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="Send mock fabric measurements over a TCP socket for app testing.")
    parser.add_argument("--host", default="127.0.0.1", help="Host to bind. Default: 127.0.0.1")
    parser.add_argument("--port", type=int, default=7001, help="TCP port to bind. Default: 7001")
    parser.add_argument("--interval", type=float, default=1.0, help="Seconds between sample lines. Default: 1.0")
    return parser


def main() -> None:
    args = build_parser().parse_args()
    samples = [
        "KG: 12.40\n",
        "M: 18.75\n",
        "18.75 metre\n",
        "42.10\n",
        "KG: 5.60\n",
    ]

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        server.bind((args.host, args.port))
        server.listen(1)
        print(f"Mock scale server listening on {args.host}:{args.port}")
        while True:
            connection, address = server.accept()
            with connection:
                print(f"Client connected from {address[0]}:{address[1]}")
                try:
                    while True:
                        for sample in samples:
                            connection.sendall(sample.encode("utf-8"))
                            print(f"Sent: {sample.strip()}")
                            time.sleep(args.interval)
                except (ConnectionAbortedError, ConnectionResetError, BrokenPipeError):
                    print("Client disconnected")


if __name__ == "__main__":
    main()