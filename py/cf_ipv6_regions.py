import ipaddress
import ssl
import socket
import sys
import time
import threading
from queue import Queue
from datetime import datetime


TEST_TIMEOUT = 3
TEST_PORT = 443
MAX_THREADS = 8
TOP_NODES = 20
SPEED_TEST_HOST = "speed.cloudflare.com"
SPEED_TEST_PATH = "/__down?bytes=1000000"
SPEED_TEST_MAX_BYTES = 1_000_000
SPEED_TEST_MAX_SECONDS = 5

CLOUDFLARE_IPV6_RANGES = [
    "2400:cb00::/32",
    "2606:4700::/32",
    "2803:f800::/32",
    "2405:b500::/32",
    "2405:8100::/32",
    "2a06:98c0::/29",
    "2c0f:f248::/32",
]

REGIONS = {
    "JPv6": {
        "output": "JPv6.txt",
        "tag": "jpv6",
        "label": "【东京IPv6】 JP",
        "seed": 11,
    },
    "USv6": {
        "output": "USv6.txt",
        "tag": "usv6",
        "label": "【美国IPv6】 US",
        "seed": 37,
    },
    "SGv6": {
        "output": "SGv6.txt",
        "tag": "sgv6",
        "label": "【新加坡IPv6】 SG",
        "seed": 73,
    },
}


def format_speed(bytes_per_second):
    if not bytes_per_second:
        return "0.00MB/s"
    return f"{bytes_per_second / 1024 / 1024:.2f}MB/s"


def format_line(node, config):
    latency = node.get("response_time_ms")
    latency_text = "timeout" if latency is None else f"{latency}ms"
    speed_text = format_speed(node.get("speed_bytes_per_second"))
    return (
        f"{node['ip']}#{config['tag']} {config['label']} "
        f"延时{latency_text} 速度{speed_text}"
    )


def candidate_addresses(seed, per_range=12):
    addresses = []
    for range_index, cidr in enumerate(CLOUDFLARE_IPV6_RANGES):
        network = ipaddress.ip_network(cidr)
        step = 0x100000000 + seed + range_index * 0x10000
        for i in range(1, per_range + 1):
            offset = step * i
            if offset >= network.num_addresses:
                offset = seed + range_index * 257 + i
            addresses.append(str(network.network_address + offset))
    return addresses


class CloudflareIPv6Tester:
    def __init__(self, config):
        self.config = config
        self.nodes = set(candidate_addresses(config["seed"]))
        self.results = []
        self.lock = threading.Lock()

    def measure_download_speed(self, connected_socket):
        context = ssl.create_default_context()
        with context.wrap_socket(
            connected_socket,
            server_hostname=SPEED_TEST_HOST,
        ) as tls_socket:
            tls_socket.settimeout(TEST_TIMEOUT)
            request = (
                f"GET {SPEED_TEST_PATH} HTTP/1.1\r\n"
                f"Host: {SPEED_TEST_HOST}\r\n"
                "User-Agent: CloudflareIPv6Tester/1.0\r\n"
                "Connection: close\r\n\r\n"
            )
            tls_socket.sendall(request.encode("ascii"))

            body_started = False
            pending = b""
            downloaded = 0
            start_time = time.time()

            while downloaded < SPEED_TEST_MAX_BYTES:
                if time.time() - start_time > SPEED_TEST_MAX_SECONDS:
                    break
                chunk = tls_socket.recv(65536)
                if not chunk:
                    break

                if not body_started:
                    pending += chunk
                    header_end = pending.find(b"\r\n\r\n")
                    if header_end == -1:
                        continue
                    body_started = True
                    downloaded += len(pending[header_end + 4:])
                    pending = b""
                else:
                    downloaded += len(chunk)

            elapsed = max(time.time() - start_time, 0.001)
            return downloaded / elapsed if downloaded else 0

    def test_node_speed(self, ip):
        try:
            start_time = time.time()
            with socket.socket(socket.AF_INET6, socket.SOCK_STREAM) as s:
                s.settimeout(TEST_TIMEOUT)
                result = s.connect_ex((ip, TEST_PORT, 0, 0))
                if result == 0:
                    response_time = (time.time() - start_time) * 1000
                    try:
                        speed_bytes_per_second = self.measure_download_speed(s)
                    except Exception:
                        speed_bytes_per_second = 0
                    return {
                        "ip": ip,
                        "reachable": True,
                        "response_time_ms": int(response_time),
                        "speed_bytes_per_second": speed_bytes_per_second,
                        "timestamp": datetime.now().isoformat(),
                    }
                return {
                    "ip": ip,
                    "reachable": False,
                    "response_time_ms": None,
                    "speed_bytes_per_second": 0,
                    "timestamp": datetime.now().isoformat(),
                }
        except Exception as exc:
            return {
                "ip": ip,
                "reachable": False,
                "response_time_ms": None,
                "speed_bytes_per_second": 0,
                "error": str(exc),
                "timestamp": datetime.now().isoformat(),
            }

    def worker(self, queue):
        while not queue.empty():
            ip = queue.get()
            try:
                result = self.test_node_speed(ip)
                with self.lock:
                    self.results.append(result)
            finally:
                queue.task_done()

    def test_all_nodes(self):
        queue = Queue()
        for ip in self.nodes:
            queue.put(ip)

        threads = []
        for _ in range(min(MAX_THREADS, len(self.nodes))):
            thread = threading.Thread(target=self.worker, args=(queue,))
            thread.start()
            threads.append(thread)

        for thread in threads:
            thread.join()

    def sorted_results(self):
        reachable_nodes = [
            node for node in self.results
            if node["reachable"] and node["response_time_ms"] is not None
        ]
        return sorted(
            reachable_nodes,
            key=lambda node: (
                -node.get("speed_bytes_per_second", 0),
                node["response_time_ms"],
            ),
        )

    def output_results(self, results):
        if results:
            nodes = results[:TOP_NODES]
        else:
            print(
                "No reachable IPv6 node found; writing candidate list.",
                file=sys.stderr,
            )
            nodes = [
                {
                    "ip": ip,
                    "response_time_ms": None,
                    "speed_bytes_per_second": 0,
                }
                for ip in sorted(self.nodes)[:TOP_NODES]
            ]

        for node in nodes:
            print(format_line(node, self.config))

    def run(self):
        self.test_all_nodes()
        self.output_results(self.sorted_results())


def run_region(region_key):
    if region_key not in REGIONS:
        raise ValueError(f"Unknown IPv6 region: {region_key}")
    CloudflareIPv6Tester(REGIONS[region_key]).run()


def run_all_regions(region_keys=None):
    keys = region_keys or ("JPv6", "USv6", "SGv6")
    for key in keys:
        if key not in REGIONS:
            raise ValueError(f"Unknown IPv6 region: {key}")
        CloudflareIPv6Tester(REGIONS[key]).run()
